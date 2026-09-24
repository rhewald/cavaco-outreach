"""Durable delivery repository; every method releases its transaction before returning."""
import base64
import re
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from uuid import uuid4
from psycopg2.extras import Json, RealDictCursor
from outreach_recovery.generation import GenerationRepository


class DeliveryRepository(GenerationRepository):
    def prepare(self, draft_id, *, recipient, subject, hubspot_portal_id, hubspot_contact_id,
                in_reply_to=None, references=(), gmail_only=False):
        # Pilot supports one plain recipient, no cc/bcc/display names/header injection.
        if not isinstance(recipient, str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', recipient):
            raise ValueError('One plain recipient email is required')
        if not isinstance(subject, str) or not subject.strip() or len(subject)>998 or any(c in subject for c in '\r\n'):
            raise ValueError('A single-line subject is required')
        if gmail_only and (hubspot_portal_id is not None or hubspot_contact_id is not None):
            raise ValueError("Gmail-only delivery cannot specify CRM IDs")
        for value in (() if gmail_only else (hubspot_portal_id, hubspot_contact_id)):
            if not isinstance(value,str) or not re.fullmatch(r'[0-9]+',value):
                raise ValueError('Explicit HubSpot portal and contact IDs are required')
        ids=tuple(references) + ((in_reply_to,) if in_reply_to else ())
        if any(not isinstance(v,str) or not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>',v) for v in ids):
            raise ValueError('Invalid reply header')
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT conversation_id FROM outreach_pilot.drafts WHERE id=%s',(str(draft_id),))
            row=cur.fetchone()
            if not row:
                raise ValueError('Draft not found')
            cur.execute('SELECT * FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE',(row['conversation_id'],))
            conversation=cur.fetchone()
            cur.execute('SELECT * FROM outreach_pilot.drafts WHERE id=%s FOR UPDATE',(str(draft_id),))
            draft=cur.fetchone()
            if draft['state']!='pending_review' or draft['version_snapshot']!=conversation['version_counter']:
                raise ValueError('A current unreviewed draft is required')
            if conversation['gmail_thread_id'] and not in_reply_to:
                raise ValueError('An existing Gmail thread requires reply headers')
            if not gmail_only:
                cur.execute('SELECT portal_id,contact_id,contact_email FROM outreach_pilot.crm_conversation_routes WHERE conversation_id=%s', (conversation['id'],))
                route=cur.fetchone()
                if route and (route['portal_id'],route['contact_id'],route['contact_email']) != (hubspot_portal_id,hubspot_contact_id,recipient):
                    raise ValueError('Conversation has a different CRM association')
            cur.execute('SELECT email FROM outreach_pilot.mailboxes WHERE id=%s',(conversation['mailbox_id'],))
            sender=cur.fetchone()['email']
            # Reuse a previously displayed envelope; never mint new MIME on a retry.
            cur.execute('SELECT id,payload FROM outreach_pilot.delivery_envelopes WHERE draft_id=%s', (str(draft_id),))
            existing=cur.fetchone()
            if existing:
                from email.parser import BytesParser
                old=existing['payload']
                raw=base64.urlsafe_b64decode(old['raw_mime'])
                mime=BytesParser(policy=SMTP).parsebytes(raw)
                expected=(recipient,subject,sender,draft['body'],hubspot_portal_id,hubspot_contact_id,
                          in_reply_to or '', ' '.join(references or ((in_reply_to,) if in_reply_to else ())))
                actual=(old['to'],old['subject'],old['from'],old['body'],old['hubspot_portal_id'],old['hubspot_contact_id'],
                        str(mime.get('In-Reply-To','')),str(mime.get('References','')))
                if actual!=expected:
                    raise ValueError('Existing delivery envelope differs; create a new reviewed draft')
                return existing['id']
            msg=EmailMessage(policy=SMTP)
            message_id=f'<{uuid4()}@{sender.split("@")[-1]}>'
            msg['From']=sender; msg['To']=recipient; msg['Subject']=subject
            msg['Message-ID']=message_id; msg['Date']=formatdate(localtime=False,usegmt=True)
            if in_reply_to:
                msg['In-Reply-To']=in_reply_to
                msg['References']=' '.join(references or (in_reply_to,))
            msg.set_content(draft['body'])
            payload=dict(mailbox_id=str(conversation['mailbox_id']), **{'from':sender,'to':recipient},
                         subject=subject,body=draft['body'],mime_message_id=message_id,
                         raw_mime=base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii'),
                         thread_id=conversation['gmail_thread_id'],hubspot_portal_id=hubspot_portal_id,
                         hubspot_contact_id=hubspot_contact_id)
            cur.execute('INSERT INTO outreach_pilot.delivery_envelopes(draft_id,payload) VALUES(%s,%s) RETURNING id',
                        (str(draft_id),Json(payload)))
            return cur.fetchone()['id']

    @staticmethod
    def _event(cur, op, event):
        cur.execute('INSERT INTO outreach_pilot.delivery_attempts(operation_id,lease_token,event) VALUES(%s,%s,%s)',
                    (op['id'],op['lease_token'],event))

    @staticmethod
    def _locked(cur, operation_id, skip=False):
        cur.execute('SELECT conversation_id,draft_id FROM outreach_pilot.delivery_operations WHERE id=%s',(str(operation_id),))
        ids=cur.fetchone()
        if not ids:
            return None
        suffix=' SKIP LOCKED' if skip else ''
        cur.execute('SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE'+suffix,(ids['conversation_id'],))
        if not cur.fetchone():
            return None
        cur.execute('SELECT id FROM outreach_pilot.drafts WHERE id=%s FOR UPDATE'+suffix,(ids['draft_id'],))
        if not cur.fetchone():
            return None
        cur.execute('SELECT *,lease_until>clock_timestamp() AS lease_active FROM outreach_pilot.delivery_operations WHERE id=%s FOR UPDATE'+suffix,(str(operation_id),))
        return cur.fetchone()

    @staticmethod
    def _owned(op, claim):
        return bool(op and op['state']=='processing' and op['lease_active'] and op['lease_token']==claim['lease_token'])

    @staticmethod
    def _fresh(cur, op):
        cur.execute('SELECT d.state,d.version_snapshot,c.version_counter FROM outreach_pilot.drafts d JOIN outreach_pilot.conversations c ON c.id=d.conversation_id WHERE d.id=%s',(op['draft_id'],))
        d=cur.fetchone()
        return d['state']=='approved' and d['version_snapshot']==d['version_counter']==op['conversation_version']

    def claim(self, *, kind, operation_id=None, lease_seconds=120, portal_id=None):
        if kind not in ('gmail_send','hubspot_log') or not 10<=lease_seconds<=3600:
            raise ValueError('Invalid delivery claim')
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''SELECT id FROM outreach_pilot.delivery_operations
                WHERE kind=%s AND (%s::uuid IS NULL OR id=%s::uuid)
                AND (%s::text IS NULL OR payload->>'hubspot_portal_id'=%s)
                AND ((state IN ('pending','reconciliation_required') AND next_attempt_at<=clock_timestamp())
                  OR (state='processing' AND lease_until<=clock_timestamp()))
                ORDER BY next_attempt_at,id LIMIT 100''',(kind,operation_id,operation_id,portal_id,portal_id))
            candidates=cur.fetchall()
            for candidate in candidates:
                op=self._locked(cur,candidate['id'],skip=True)
                if not op:
                    continue
                cur.execute("SELECT ((%s IN ('pending','reconciliation_required') AND %s<=clock_timestamp()) OR (%s='processing' AND %s<=clock_timestamp())) AS due",
                            (op['state'],op['next_attempt_at'],op['state'],op['lease_until']))
                if not cur.fetchone()['due']:
                    continue
                if op['depends_on']:
                    cur.execute('SELECT state FROM outreach_pilot.delivery_operations WHERE id=%s',(op['depends_on'],))
                    if cur.fetchone()['state']!='completed':
                        continue
                op['lease_token']=str(uuid4())
                if op['attempts']>=8:
                    cur.execute("UPDATE outreach_pilot.delivery_operations SET state='manual_review',lease_token=NULL,lease_until=NULL WHERE id=%s",(op['id'],))
                    self._event(cur,op,'manual_review')
                    continue
                recovery=op['state']!='pending' or (kind=='gmail_send' and op['started_at'] is not None)
                if not recovery and kind=='gmail_send' and not self._fresh(cur,op):
                    cur.execute("UPDATE outreach_pilot.delivery_operations SET state='superseded' WHERE id=%s",(op['id'],))
                    self._event(cur,op,'superseded')
                    continue
                cur.execute("""UPDATE outreach_pilot.delivery_operations SET state='processing',lease_token=%s,
                    lease_until=clock_timestamp()+make_interval(secs=>%s),attempts=attempts+1 WHERE id=%s RETURNING *""",
                            (op['lease_token'],lease_seconds,op['id']))
                op=cur.fetchone()
                op['recovery']=recovery
                self._event(cur,op,'claimed_reconcile' if recovery else 'claimed_dispatch')
                return op
        return None

    def begin(self, claim):
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            op=self._locked(cur,claim['id'])
            if not self._owned(op,claim) or claim['recovery'] or (op['kind']=='gmail_send' and op['started_at'] is not None):
                return False
            cur.execute("SELECT 1 FROM outreach_pilot.delivery_attempts WHERE operation_id=%s AND lease_token=%s AND event='started'",(op['id'],op['lease_token']))
            if cur.fetchone():
                return False
            if op['kind']=='gmail_send' and not self._fresh(cur,op):
                cur.execute("UPDATE outreach_pilot.delivery_operations SET state='superseded',lease_token=NULL,lease_until=NULL WHERE id=%s",(op['id'],))
                self._event(cur,op,'superseded')
                return False
            p=op['payload']
            if op['kind']=='gmail_send' and p.get('hubspot_portal_id') and p.get('hubspot_contact_id'):
                from outreach_recovery.crm_handoff import register_route
                cur.execute("SELECT reviewer FROM outreach_pilot.review_events WHERE draft_id=%s AND decision='approved'", (op['draft_id'],))
                register_route(cur,op['conversation_id'],portal_id=p['hubspot_portal_id'],
                    contact_id=p['hubspot_contact_id'],contact_email=p['to'],mailbox_email=p['from'],
                    reviewer=cur.fetchone()['reviewer'],approval_reference='delivery:'+str(op['id']))
            cur.execute('UPDATE outreach_pilot.delivery_operations SET started_at=coalesce(started_at,clock_timestamp()) WHERE id=%s',(op['id'],))
            self._event(cur,op,'started')
            return True

    def defer(self, claim, event, *, rejected=False, safe_to_retry=False):
        if event not in ('uncertain','not_found_yet','lookup_failed','rejected'):
            raise ValueError('Invalid outcome')
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            op=self._locked(cur,claim['id'])
            if not self._owned(op,claim):
                return False
            state='reconciliation_required'
            if rejected:
                state='rejected'
                # Only CRM non-acceptance with explicit evidence permits another create.
                if safe_to_retry and op['kind']=='hubspot_log':
                    state='pending'; event='retry_rejected'
            if op['attempts']>=8 and state in ('pending','reconciliation_required'):
                state='manual_review'
            delay=min(3600,10*2**min(op['attempts']-1,9))
            cur.execute('''UPDATE outreach_pilot.delivery_operations SET state=%s,lease_token=NULL,lease_until=NULL,
                next_attempt_at=clock_timestamp()+make_interval(secs=>%s) WHERE id=%s''',(state,delay,op['id']))
            self._event(cur,op,event)
            return True

    def complete(self, claim, provider_id, thread_id=None):
        if not isinstance(provider_id,str) or not provider_id.strip():
            raise ValueError('Provider identifier required')
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            op=self._locked(cur,claim['id'])
            if not self._owned(op,claim):
                return False
            if op['kind']=='gmail_send':
                p=op['payload']
                cur.execute('SELECT version_counter FROM outreach_pilot.conversations WHERE id=%s',(op['conversation_id'],))
                observed=cur.fetchone()['version_counter']
                if observed!=op['conversation_version']:
                    cur.execute('''INSERT INTO outreach_pilot.delivery_followups(operation_id,reason,observed_version)
                        VALUES(%s,'conversation_changed_during_send',%s) ON CONFLICT DO NOTHING''',(op['id'],observed))
                cur.execute('SELECT * FROM outreach_pilot.messages WHERE mailbox_id=%s AND gmail_message_id=%s',(op['mailbox_id'],provider_id))
                existing=cur.fetchone()
                if existing:
                    if (existing['conversation_id']!=op['conversation_id'] or existing['direction']!='outbound'
                        or existing['mime_message_id']!=p['mime_message_id'] or existing['body_text']!=p['body']):
                        raise ValueError('Conflicting provider message')
                else:
                    cur.execute('UPDATE outreach_pilot.conversations SET version_counter=version_counter+1,updated_at=clock_timestamp() WHERE id=%s RETURNING version_counter',(op['conversation_id'],))
                    version=cur.fetchone()['version_counter']
                    cur.execute("""INSERT INTO outreach_pilot.messages(mailbox_id,conversation_id,gmail_message_id,mime_message_id,direction,body_text,received_at,conversation_version)
                        VALUES(%s,%s,%s,%s,'outbound',%s,clock_timestamp(),%s)""",
                        (op['mailbox_id'],op['conversation_id'],provider_id,p['mime_message_id'],p['body'],version))
                    cur.execute("UPDATE outreach_pilot.drafts SET state='superseded' WHERE conversation_id=%s AND id<>%s AND version_snapshot<%s AND state IN ('pending_review','approved')",(op['conversation_id'],op['draft_id'],version))
                    cur.execute("UPDATE outreach_pilot.reply_jobs SET state='superseded' WHERE conversation_id=%s AND version_snapshot<%s AND state IN ('pending','processing')",(op['conversation_id'],version))
                # If a reply arrived during the network call, record the actual send,
                # even though the draft was superseded. Never erase real provider facts.
                cur.execute("UPDATE outreach_pilot.drafts SET state='sent' WHERE id=%s",(op['draft_id'],))
                if thread_id:
                    cur.execute('UPDATE outreach_pilot.conversations SET gmail_thread_id=coalesce(gmail_thread_id,%s) WHERE id=%s',(thread_id,op['conversation_id']))
                if p.get('hubspot_portal_id') and p.get('hubspot_contact_id'):
                    from outreach_recovery.crm_handoff import register_route
                    cur.execute("SELECT reviewer FROM outreach_pilot.review_events WHERE draft_id=%s AND decision='approved'", (op['draft_id'],))
                    reviewer=cur.fetchone()['reviewer']
                    register_route(cur,op['conversation_id'],portal_id=p['hubspot_portal_id'],
                        contact_id=p['hubspot_contact_id'],contact_email=p['to'],mailbox_email=p['from'],
                        reviewer=reviewer,approval_reference='delivery:'+str(op['id']))
                    crm=dict(p, gmail_message_id=provider_id,gmail_thread_id=thread_id or p.get('thread_id'))
                    cur.execute("""INSERT INTO outreach_pilot.delivery_operations(draft_id,envelope_id,conversation_id,mailbox_id,conversation_version,kind,depends_on,payload)
                        VALUES(%s,%s,%s,%s,%s,'hubspot_log',%s,%s) ON CONFLICT(draft_id,kind) DO NOTHING""",
                        (op['draft_id'],op['envelope_id'],op['conversation_id'],op['mailbox_id'],op['conversation_version'],op['id'],Json(crm)))
            cur.execute("""UPDATE outreach_pilot.delivery_operations SET state='completed',provider_id=%s,provider_thread_id=%s,
                completed_at=clock_timestamp(),lease_token=NULL,lease_until=NULL WHERE id=%s""",(provider_id,thread_id,op['id']))
            self._event(cur,op,'found' if claim['recovery'] else 'accepted')
            return True
