"""Bounded read-only Gmail ingestion and verification of known accepted sends."""
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from outreach_recovery.gmail_adapter import decode_raw, fingerprint


def parse_reply(data, *, expected_sender, mailbox_email, thread_id):
    if data.get('threadId') != thread_id or 'SENT' in data.get('labelIds', []):
        raise ValueError('Not an inbound message in the expected thread')
    message = BytesParser(policy=policy.default).parsebytes(decode_raw(data['raw']))
    if message.defects:
        raise ValueError('Malformed MIME')
    for field in ('From', 'To', 'Message-ID', 'In-Reply-To', 'References', 'Subject'):
        if len(message.get_all(field, [])) > 1:
            raise ValueError('Duplicate routing header')
    senders = getaddresses(message.get_all('From', []))
    recipients = getaddresses(message.get_all('To', []))
    if len(senders) != 1 or senders[0][1].casefold() != expected_sender.casefold():
        raise ValueError('Unexpected sender')
    if mailbox_email.casefold() not in {a.casefold() for _, a in recipients}:
        raise ValueError('Unexpected recipient')
    part = message.get_body(preferencelist=('plain',)) if message.is_multipart() else message
    if part is None or part.get_content_type() != 'text/plain':
        raise ValueError('Plain text body required; manual review needed')
    body = part.get_content()
    if not isinstance(body, str) or not body.strip() or len(body) > 100000:
        raise ValueError('Invalid body size')
    reply_ids = re.findall(r'<[^\s<>]+@[^\s<>]+>', ' '.join(str(message.get(k, '')) for k in ('In-Reply-To','References')))
    subject = str(message.get('Subject', ''))
    if len(subject) > 998 or any(c in subject for c in '\r\n'):
        raise ValueError('Invalid subject')
    return dict(sender=senders[0][1], recipient=mailbox_email, subject=subject,
                gmail_message_id=data['id'], gmail_thread_id=thread_id,
                mime_message_id=str(message.get('Message-ID', '')) or None,
                reply_ids=list(dict.fromkeys(reply_ids)), body=body,
                received_at=datetime.fromtimestamp(int(data['internalDate'])/1000, timezone.utc))


def fetch_message(adapter, message_id, *, timeout_seconds=30):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', message_id):
        raise ValueError('Invalid Gmail ID')
    deadline = adapter._deadline(timeout_seconds)
    with adapter._session(deadline) as client:
        adapter._profile(client, deadline)
        response = adapter._request(client, deadline, 'GET', '/messages/'+message_id, params={'format':'raw'})
        if response.status_code != 200:
            raise ValueError('Gmail message unavailable')
        data = response.json()
        if data.get('id') != message_id:
            raise ValueError('Provider identity mismatch')
        return data


def verify_accepted(adapter, payload, provider_id, provider_thread_id):
    """Only use IDs durably recorded from Gmail acceptance, never search guesses.

    Unknown/ambiguous sends must continue through reconciliation/manual review.
    The approved envelope remains immutable; observed MIME IDs are separate facts.
    """
    expected = adapter._validate(payload)
    data = fetch_message(adapter, provider_id)
    actual = fingerprint(decode_raw(data['raw']))
    if 'SENT' not in data.get('labelIds', []) or data.get('threadId') != provider_thread_id:
        raise ValueError('Sent message/thread mismatch')
    # Ignore only Message-ID: Gmail can rewrite it. All other pinned fields match.
    if actual[:3]+actual[4:] != expected[:3]+expected[4:]:
        raise ValueError('Accepted message differs from approved content')
    return actual[3]


def ingest_message(repository, parsed):
    with repository.connection() as conn, conn.cursor() as cur:
        from outreach_recovery.crm_handoff import retain_inbound_headers, handoff_ingested
        retain_inbound_headers(cur, parsed)
        cur.execute('SELECT * FROM outreach_pilot.ingest_reply(%s,%s,%s,%s,%s,%s,%s)',
                    (parsed['mailbox_id'],parsed['gmail_message_id'],parsed['gmail_thread_id'],
                     parsed['mime_message_id'],parsed['reply_ids'],parsed['body'],parsed['received_at']))
        result = cur.fetchone()
        handoff_ingested(cur, result[1])
        return result
