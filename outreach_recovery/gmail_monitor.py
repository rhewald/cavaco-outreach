"""Read-only enrolled-thread monitor with leased, durable Gmail history checkpoints."""
import hashlib
import json
import re
from uuid import uuid4
from psycopg2.extras import RealDictCursor, Json
from outreach_recovery.generation import GenerationRepository
from outreach_recovery.gmail_inbound import parse_reply, ingest_in_transaction


class SyncError(Exception):
    def __init__(self, code, retry_after=0):
        self.code=code
        self.retry_after=min(3600,max(0,retry_after))
        super().__init__(code)


class HistoryExpired(SyncError):
    def __init__(self):super().__init__('history_expired')


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',value):
        raise SyncError('invalid_response')
    return value


def cursor_id(value):
    if not isinstance(value,str) or not value.isdigit():raise SyncError('invalid_response')
    return value


class GmailHistorySource:
    def __init__(self, adapter):self.adapter=adapter
    def get(self,path,params=None,*,history=False,missing=False):
        a=self.adapter;deadline=a._deadline(30)
        with a._session(deadline) as client:
            a._profile(client,deadline)
            response=a._request(client,deadline,'GET',path,params=params)
            if history and response.status_code==400 and params.get('pageToken'):
                raise HistoryExpired() # A stale page token requires the same scoped resync.
            if response.status_code==404:
                if history:raise HistoryExpired()
                if missing:return None
            if response.status_code!=200:
                value=response.headers.get('Retry-After','0')
                raise SyncError('provider_http_'+str(response.status_code),int(value) if value.isdigit() else 0)
            return response.json()
    def baseline(self):return cursor_id(self.get('/profile')['historyId'])
    def history(self,start,page):
        params={'startHistoryId':cursor_id(start),'historyTypes':'messageAdded','maxResults':100}
        if page:params['pageToken']=page
        return self.get('/history',params,history=True)
    def thread(self,thread):
        return self.get('/threads/'+identifier(thread),{'format':'minimal','fields':'id,messages(id,threadId,labelIds)'},missing=True)
    def message(self,message):
        return self.get('/messages/'+identifier(message),{'format':'raw'},missing=True)


class MonitorRepository(GenerationRepository):
    def __init__(self,dsn,mailbox_id):
        super().__init__(dsn);self.mailbox_id=str(mailbox_id)
    def routes(self):
        with self.connection() as c,c.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("""SELECT c.gmail_thread_id AS thread_id,r.contact_email,r.mailbox_email
                FROM outreach_pilot.crm_conversation_routes r JOIN outreach_pilot.conversations c ON c.id=r.conversation_id
                WHERE c.mailbox_id=%s AND c.gmail_thread_id IS NOT NULL ORDER BY c.gmail_thread_id""",(self.mailbox_id,))
            return [dict(row) for row in q.fetchall()]
    def claim(self):
        with self.connection() as c,c.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("INSERT INTO outreach_pilot.gmail_monitor_state(mailbox_id) VALUES(%s) ON CONFLICT DO NOTHING",(self.mailbox_id,))
            q.execute("""UPDATE outreach_pilot.gmail_monitor_state SET lease_token=%s,lease_until=clock_timestamp()+interval '120 seconds'
                WHERE mailbox_id=%s AND next_poll_at<=clock_timestamp()
                 AND (lease_until IS NULL OR lease_until<=clock_timestamp()) RETURNING *""",(str(uuid4()),self.mailbox_id))
            row=q.fetchone();return dict(row) if row else None
    def owned(self,q,claim):
        q.execute("""SELECT 1 FROM outreach_pilot.gmail_monitor_state WHERE mailbox_id=%s AND lease_token=%s
            AND lease_until>clock_timestamp() FOR UPDATE""",(self.mailbox_id,claim['lease_token']))
        if not q.fetchone():raise SyncError('lease_lost')
    def checkpoint(self,claim,**updates):
        allowed={'history_id','page_token','route_fingerprint','resync_routes','resync_index','resync_history_id'}
        if set(updates)-allowed:raise ValueError('Invalid checkpoint')
        with self.connection() as c,c.cursor() as q:
            self.owned(q,claim)
            values=[Json(v) if k=='resync_routes' and v is not None else v for k,v in updates.items()]
            sets=','.join(k+'=%s' for k in updates)
            q.execute('UPDATE outreach_pilot.gmail_monitor_state SET '+sets+",lease_until=clock_timestamp()+interval '120 seconds' WHERE mailbox_id=%s",(*values,self.mailbox_id))
        claim.update(updates)
    def renew(self,claim):self.checkpoint(claim,resync_index=claim['resync_index'])
    def seen(self,message):
        with self.connection() as c,c.cursor() as q:
            q.execute('SELECT 1 FROM outreach_pilot.gmail_monitor_messages WHERE mailbox_id=%s AND gmail_message_id=%s',(self.mailbox_id,message))
            return bool(q.fetchone())
    def record(self,claim,message,thread,outcome,parsed=None):
        with self.connection() as c,c.cursor() as q:
            self.owned(q,claim)
            if parsed:
                result=ingest_in_transaction(q,dict(parsed,mailbox_id=self.mailbox_id))
                # Do not consume unmatched receipts: a later route/send may resolve them.
                if result[0] not in ('ingested','duplicate'):raise SyncError('ingestion_unresolved')
                outcome=result[0]
            q.execute("""INSERT INTO outreach_pilot.gmail_monitor_messages(mailbox_id,gmail_message_id,thread_id,outcome)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",(self.mailbox_id,message,thread,outcome))
            return outcome
    def finish(self,claim,error=None,retry_after=0):
        with self.connection() as c,c.cursor() as q:
            self.owned(q,claim)
            delay=max(30*2**min(claim['failures'],5),retry_after) if error else 30
            q.execute("""UPDATE outreach_pilot.gmail_monitor_state SET lease_token=NULL,lease_until=NULL,
                failures=CASE WHEN %s THEN failures+1 ELSE 0 END,last_error=%s,
                last_success_at=CASE WHEN %s THEN last_success_at ELSE clock_timestamp() END,
                next_poll_at=clock_timestamp()+make_interval(secs=>%s) WHERE mailbox_id=%s""",
                (bool(error),error,bool(error),delay,self.mailbox_id))


class GmailMonitor:
    def __init__(self,repository,source,max_messages=200,max_pages=20):
        self.repo=repository;self.source=source;self.max_messages=max_messages;self.max_pages=max_pages
    def process(self,claim,items,routes):
        for summary in items:
            thread=summary.get('threadId')
            if thread not in routes:continue # No body fetched for unrelated mail.
            message=identifier(summary.get('id'))
            if self.repo.seen(message):continue
            if self.remaining<=0:return False
            self.remaining-=1;self.repo.renew(claim)
            if 'SENT' in summary.get('labelIds',[]) or 'DRAFT' in summary.get('labelIds',[]):
                self.repo.record(claim,message,thread,'sent');continue
            raw=self.source.message(message)
            if raw is None:self.repo.record(claim,message,thread,'deleted');continue
            if raw.get('id')!=message or raw.get('threadId')!=thread:raise SyncError('message_identity_mismatch')
            if 'SENT' in raw.get('labelIds',[]) or 'DRAFT' in raw.get('labelIds',[]):
                self.repo.record(claim,message,thread,'sent');continue
            route=routes[thread]
            try:
                parsed=parse_reply(raw,expected_sender=route['contact_email'],mailbox_email=route['mailbox_email'],thread_id=thread)
            except (ValueError,KeyError,TypeError):
                self.repo.record(claim,message,thread,'quarantined');continue
            self.repo.record(claim,message,thread,'ingested',parsed)
        return True
    def reset(self,claim,routes,fingerprint):
        # Capture baseline BEFORE reading threads; history catches concurrent arrivals.
        baseline=self.source.baseline()
        self.repo.checkpoint(claim,resync_routes=routes,resync_index=0,resync_history_id=baseline,
                             page_token=None,route_fingerprint=fingerprint)
    def run_once(self):
        claim=self.repo.claim()
        if not claim:return 'idle'
        self.remaining=self.max_messages
        try:
            routes=self.repo.routes()
            fingerprint=hashlib.sha256(json.dumps(routes,sort_keys=True).encode()).hexdigest()
            if not claim['history_id'] or claim['route_fingerprint']!=fingerprint:
                # Preserve an in-progress initial resync across bounded cycles.
                if claim['resync_routes'] is None or claim['route_fingerprint']!=fingerprint:self.reset(claim,routes,fingerprint)
            for _ in range(self.max_pages):
                self.repo.renew(claim)
                if claim['resync_routes'] is not None:
                    snapshot=claim['resync_routes'];index=claim['resync_index']
                    if index>=len(snapshot):
                        self.repo.checkpoint(claim,history_id=claim['resync_history_id'],page_token=None,resync_routes=None,
                                             resync_history_id=None,resync_index=0)
                        continue
                    route=snapshot[index];thread=route['thread_id'];data=self.source.thread(thread)
                    if data is not None:
                        if data.get('id')!=thread:raise SyncError('thread_identity_mismatch')
                        if not self.process(claim,data.get('messages',[]),{thread:route}):break
                    self.repo.checkpoint(claim,resync_index=index+1)
                else:
                    try:data=self.source.history(claim['history_id'],claim['page_token'])
                    except HistoryExpired:
                        self.reset(claim,routes,fingerprint);continue
                    items=[a['message'] for h in data.get('history',[]) for a in h.get('messagesAdded',[])]
                    if not self.process(claim,items,{r['thread_id']:r for r in routes}):break
                    page=data.get('nextPageToken')
                    if page:
                        if not isinstance(page,str) or page==claim['page_token']:raise SyncError('invalid_pagination')
                        self.repo.checkpoint(claim,page_token=page)
                    else:
                        latest=cursor_id(data.get('historyId'))
                        if int(latest)<int(claim['history_id']):raise SyncError('history_regressed')
                        self.repo.checkpoint(claim,history_id=latest,page_token=None)
                        break
            self.repo.finish(claim)
            return 'synced'
        except Exception as exc:
            code=exc.code if isinstance(exc,SyncError) else 'sync_failed'
            try:self.repo.finish(claim,code,getattr(exc,'retry_after',0))
            except SyncError:pass
            return code
