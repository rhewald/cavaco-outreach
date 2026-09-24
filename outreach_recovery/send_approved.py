"""Explicit preview/approval CLI for one enrolled reply. Never drains the send queue."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import UUID
from psycopg2.extras import RealDictCursor
from outreach_recovery.delivery_repository import DeliveryRepository
from outreach_recovery.delivery_worker import DeliveryWorker
from outreach_recovery.review_repository import ReviewRepository
from outreach_recovery.gmail_adapter import GmailAdapter
from outreach_recovery.gmail_auth import OAuthTokenProvider


class ReplySender:
    def __init__(self,dsn):self.dsn=dsn;self.delivery=DeliveryRepository(dsn)
    def operation(self,draft_id):
        with self.delivery.connection() as c,c.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("SELECT id,state,provider_id,provider_thread_id FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='gmail_send'",(str(draft_id),))
            return q.fetchone()
    def prepare(self,draft_id):
        draft_id=str(UUID(str(draft_id)))
        with self.delivery.connection() as c,c.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("""SELECT d.*,c.version_counter,c.mailbox_id,c.gmail_thread_id,
                m.mime_message_id,m.direction,h.sender,h.recipient,h.subject,
                r.portal_id,r.contact_id,r.contact_email,r.mailbox_email,i.reply_ids
                FROM outreach_pilot.drafts d
                JOIN outreach_pilot.conversations c ON c.id=d.conversation_id
                JOIN outreach_pilot.messages m ON m.id=d.triggering_message_id AND m.conversation_id=c.id
                JOIN outreach_pilot.crm_inbound_headers h ON h.mailbox_id=m.mailbox_id AND h.gmail_message_id=m.gmail_message_id
                JOIN outreach_pilot.crm_conversation_routes r ON r.conversation_id=c.id
                LEFT JOIN outreach_pilot.inbound_receipts i ON i.mailbox_id=m.mailbox_id AND i.gmail_message_id=m.gmail_message_id
                WHERE d.id=%s""",(draft_id,))
            row=q.fetchone()
            if not row:raise ValueError('An enrolled reply with verified inbound headers is required')
            if row['direction']!='inbound' or row['sender'].casefold()!=row['contact_email'].casefold() or row['recipient'].casefold()!=row['mailbox_email'].casefold():
                raise ValueError('Reply routing does not match the approved contact')
            q.execute('SELECT id,payload FROM outreach_pilot.delivery_envelopes WHERE draft_id=%s',(draft_id,))
            envelope=q.fetchone()
        operation=self.operation(draft_id)
        recovery=bool(operation and operation['state']!='pending' and envelope)
        if not recovery and row['state'] not in ('pending_review','approved','sent'):
            raise ValueError('Draft is rejected or superseded; review the latest draft')
        if not recovery and row['state']!='sent' and row['version_snapshot']!=row['version_counter']:
            raise ValueError('Conversation changed; review the latest draft')
        if envelope is None:
            if row['state']!='pending_review':raise ValueError('This draft was approved without a send envelope; create a new review draft')
            if not row['mime_message_id'] or not row['subject'].strip():raise ValueError('Reply Message-ID and subject are required')
            subject=row['subject'] if row['subject'].lower().startswith('re:') else 'Re: '+row['subject']
            references=list(dict.fromkeys((row['reply_ids'] or [])+[row['mime_message_id']]))
            if len(references)>100:raise ValueError('Reply header chain requires manual review')
            self.delivery.prepare(draft_id,recipient=row['contact_email'],subject=subject,
                hubspot_portal_id=row['portal_id'],hubspot_contact_id=row['contact_id'],
                in_reply_to=row['mime_message_id'],references=references)
            with self.delivery.connection() as c,c.cursor(cursor_factory=RealDictCursor) as q:
                q.execute('SELECT id,payload FROM outreach_pilot.delivery_envelopes WHERE draft_id=%s',(draft_id,))
                envelope=q.fetchone()
        result={'draft_id':draft_id,'envelope_id':str(envelope['id']),'payload':envelope['payload']}
        result['digest']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return result
    def execute(self,preview,*,confirmed_digest,reviewer,gmail):
        # Re-read immediately before approval; SQL then rechecks version under locks.
        current=self.prepare(preview['draft_id'])
        if confirmed_digest!=current['digest'] or preview['digest']!=current['digest']:
            raise ValueError('Confirmation does not match the exact preview')
        existing=self.operation(current['draft_id'])
        if existing and existing['state']=='completed':return {'outcome':'already_sent',**dict(existing)}
        if existing is None or existing['state']=='pending':
            result=ReviewRepository(self.dsn).decide(current['draft_id'],'approved',reviewer,envelope_id=current['envelope_id'])
            if result not in ('approved','already_approved'):raise ValueError('Approval blocked: '+result)
        op=self.operation(current['draft_id'])
        if not op:raise ValueError('Approved send intent is unavailable')
        outcome=DeliveryWorker(self.delivery,gmail=gmail,hubspot=None).run_once('gmail_send',op['id'])
        return {'outcome':outcome,**dict(self.operation(current['draft_id']))}


def terminal_text(value):
    # Untrusted email text cannot inject ANSI commands or hidden terminal controls.
    return ''.join(c if c.isprintable() or c in '\n\t' else ('\\u%04x'%ord(c)) for c in value)


def render(preview):
    p=preview['payload']
    return '\n'.join(['='*60,'REVIEW PENDING DRAFT: '+preview['draft_id'],
        'From:    '+terminal_text(p['from']),'To:      '+terminal_text(p['to']),
        'Subject: '+terminal_text(p['subject']),'-'*60,terminal_text(p['body']),'='*60,
        'HubSpot contact: '+p['hubspot_contact_id']+' (portal '+p['hubspot_portal_id']+')',
        'Approval digest: '+preview['digest']])


def confirmed(preview,digest,*,interactive,input_fn=input):
    if digest is not None:
        if digest!=preview['digest']:raise ValueError('Confirmation digest differs from preview')
        return True
    if not interactive:return False
    return input_fn('Approve and send? [y/N]: ').strip().lower()=='y'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    send=sub.add_parser('send-approved')
    send.add_argument('draft_id',type=UUID)
    send.add_argument('--preview',action='store_true',help='Prepare and print only; no approval or send')
    send.add_argument('--confirm-digest',help='Noninteractive confirmation of a previously reviewed exact preview')
    args=parser.parse_args()
    try:
        dsn=os.environ.get('OUTREACH_DATABASE_URL')
        if not dsn:
            import pgserver
            server=pgserver.get_server(Path.home()/'.local/share/cavaco-outreach/controlled-pilot/postgres',cleanup_mode='stop')
            dsn=server.get_uri()
        sender=ReplySender(dsn);preview=sender.prepare(args.draft_id)
        print(render(preview),flush=True)
        if args.preview:return 0
        if not confirmed(preview,args.confirm_digest,interactive=sys.stdin.isatty()):
            print('Not approved. No email sent.');return 0
        p=preview['payload']
        gmail=GmailAdapter(mailbox_id=p['mailbox_id'],email=p['from'],token_provider=OAuthTokenProvider())
        result=sender.execute(preview,confirmed_digest=preview['digest'],reviewer='Rui',gmail=gmail)
        print(json.dumps(result,default=str))
        if result['state']=='completed':print('Gmail accepted this reply. CRM logging is queued independently.')
        else:print('Check the recorded state. Uncertain sends require reconciliation; do not create a replacement send.')
        return 0
    except ValueError as error:
        print('Send blocked: '+str(error));return 1
    except Exception:
        print('Operation interrupted. Inspect durable delivery state before retrying; no raw provider error is displayed.');return 1

if __name__=='__main__':raise SystemExit(main())
