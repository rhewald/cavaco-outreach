"""Gmail REST boundary: pinned plain-text MIME, no database access or mutation retries.

Tokens are supplied by a trusted local credential provider. Interactive OAuth and
credential persistence are intentionally outside this module.
"""
import base64
import math
import re
import time
from email import policy
from email.parser import BytesParser
from uuid import UUID
import httpx
from outreach_recovery.delivery_contracts import (
    MutationResult, MutationState, ReconciliationResult, ReconciliationState,
)

API = 'https://gmail.googleapis.com/gmail/v1/users/me'
MAX_RAW = 2_000_000  # Pilot plain-text messages, no attachments.


def decode_raw(value):
    if not isinstance(value,str) or not value or len(value)>MAX_RAW:
        raise ValueError('Invalid raw MIME')
    return base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)


def fingerprint(raw):
    message=BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects or message.is_multipart() or message.get_content_type()!='text/plain':
        raise ValueError('Only valid plain-text MIME is supported')
    names=('From','To','Subject','Message-ID','In-Reply-To','References')
    values=[]
    for name in names:
        headers=message.get_all(name,[])
        if len(headers)>1 or any(getattr(h,'defects',()) for h in headers):
            raise ValueError('Ambiguous MIME header')
        values.append(str(headers[0]) if headers else '')
    if any(message.get_all(name) for name in ('Cc','Bcc','Resent-To','Resent-Cc','Resent-Bcc')):
        raise ValueError('Additional recipients are unsupported')
    if not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>',values[3]):
        raise ValueError('Message-ID required')
    body=message.get_content().replace('\r\n','\n')
    return tuple(values)+(body,)


class GmailAdapter:
    def __init__(self, *, mailbox_id, email, token_provider, transport=None):
        self.mailbox_id=str(UUID(str(mailbox_id)))
        if not isinstance(email,str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+',email):
            raise ValueError('Explicit primary mailbox email required')
        if not callable(token_provider):
            raise ValueError('Credential provider required')
        self.email=email
        self.token_provider=token_provider
        self.transport=transport  # Test-only injection; never configure via payload.

    def _validate(self,payload):
        if str(payload.get('mailbox_id'))!=self.mailbox_id or payload.get('from')!=self.email:
            raise ValueError('Mailbox mismatch')
        if not isinstance(payload.get('to'),str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+',payload['to']):
            raise ValueError('One plain recipient required')
        fp=fingerprint(decode_raw(payload['raw_mime']))
        expected_body=payload['body'].replace('\r\n','\n')
        if not expected_body.endswith('\n'): expected_body+='\n'
        if fp[:4]!=(self.email,payload['to'],payload['subject'],payload['mime_message_id']) or fp[6]!=expected_body:
            raise ValueError('Pinned MIME does not match approved envelope')
        thread=payload.get('thread_id')
        if thread is not None and (not isinstance(thread,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',thread) or not fp[4] or not fp[5]):
            raise ValueError('Thread requires valid ID and reply headers')
        return fp

    @staticmethod
    def _remaining(deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0: raise TimeoutError('Provider deadline exhausted')
        return remaining

    def _session(self,deadline):
        # token_provider must respect this budget; never log its result/exceptions.
        token=self.token_provider(timeout_seconds=self._remaining(deadline))
        if not isinstance(token,str) or not token or any(c.isspace() for c in token):
            raise ValueError('Credential unavailable')
        return httpx.Client(headers={'Authorization':'Bearer '+token},
            transport=self.transport or httpx.HTTPTransport(retries=0),
            follow_redirects=False,trust_env=False)

    def _request(self,client,deadline,method,path,**kwargs):
        return client.request(method,API+path,timeout=self._remaining(deadline),**kwargs)

    def _profile(self,client,deadline):
        response=self._request(client,deadline,'GET','/profile')
        if response.status_code!=200 or response.json().get('emailAddress','').casefold()!=self.email.casefold():
            raise ValueError('Authenticated mailbox mismatch or unavailable')

    @staticmethod
    def _deadline(timeout_seconds):
        if not isinstance(timeout_seconds,(int,float)) or not math.isfinite(timeout_seconds) or timeout_seconds<=0:
            raise ValueError('Positive bounded timeout required')
        return time.monotonic()+timeout_seconds

    def send(self,payload,*,operation_id,timeout_seconds):
        invoked=False
        try:
            UUID(str(operation_id))
            self._validate(payload)
            deadline=self._deadline(timeout_seconds)
            with self._session(deadline) as client:
                self._profile(client,deadline)
                body={'raw':payload['raw_mime']}  # Forward the stored bytes unchanged.
                if payload.get('thread_id'): body['threadId']=payload['thread_id']
                self._remaining(deadline)
                invoked=True
                response=self._request(client,deadline,'POST','/messages/send',json=body)
                # Only explicit client rejections; no Gmail rejection auto-retries.
                if response.status_code in (400,401,403,404,413,422,429):
                    return MutationResult(MutationState.REJECTED)
                if response.status_code!=200:
                    return MutationResult(MutationState.UNCERTAIN)
                result=response.json()
                message_id=result.get('id'); thread_id=result.get('threadId')
                if not isinstance(message_id,str) or not message_id or not isinstance(thread_id,str) or not thread_id:
                    return MutationResult(MutationState.UNCERTAIN)
                if payload.get('thread_id') and thread_id!=payload['thread_id']:
                    return MutationResult(MutationState.UNCERTAIN)
                return MutationResult(MutationState.ACCEPTED,message_id,thread_id)
        except Exception:
            # No raw exception messages may escape: they can include credentials/MIME.
            return MutationResult(MutationState.UNCERTAIN if invoked else MutationState.REJECTED)

    def reconcile(self,payload,*,operation_id,timeout_seconds):
        try:
            UUID(str(operation_id))
            expected=self._validate(payload)
            deadline=self._deadline(timeout_seconds)
            with self._session(deadline) as client:
                self._profile(client,deadline)
                page_token=None; seen_pages=set(); seen_ids=set(); found=[]
                for _ in range(10):
                    params={'q':'rfc822msgid:'+payload['mime_message_id'][1:-1],
                            'labelIds':'SENT','maxResults':100,'includeSpamTrash':'true'}
                    if page_token: params['pageToken']=page_token
                    response=self._request(client,deadline,'GET','/messages',params=params)
                    if response.status_code!=200: raise ValueError('Lookup failed')
                    data=response.json()
                    items=data.get('messages',[])
                    if not isinstance(items,list): raise ValueError('Invalid lookup result')
                    for summary in items:
                        identifier=summary['id']
                        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',identifier):
                            raise ValueError('Invalid provider identifier')
                        if identifier in seen_ids: continue
                        seen_ids.add(identifier)
                        detail=self._request(client,deadline,'GET','/messages/'+identifier,params={'format':'raw'})
                        if detail.status_code!=200: raise ValueError('Message unavailable')
                        actual=detail.json()
                        if actual.get('id')!=identifier or 'SENT' not in actual.get('labelIds',[]):
                            raise ValueError('Invalid sent identity')
                        if fingerprint(decode_raw(actual['raw']))!=expected:
                            raise ValueError('Search hit differs from approved message')
                        thread=actual.get('threadId')
                        if not isinstance(thread,str) or not thread or (payload.get('thread_id') and thread!=payload['thread_id']):
                            raise ValueError('Thread mismatch')
                        found.append((identifier,thread))
                        if len(found)>1: raise ValueError('Multiple matching sent messages')
                    page_token=data.get('nextPageToken')
                    if not page_token:
                        if found: return ReconciliationResult(ReconciliationState.FOUND,*found[0])
                        return ReconciliationResult(ReconciliationState.NOT_FOUND_YET)
                    if not isinstance(page_token,str) or page_token in seen_pages:
                        raise ValueError('Invalid pagination')
                    seen_pages.add(page_token)
                raise ValueError('Lookup exceeds bounded page budget')
        except Exception:
            return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
