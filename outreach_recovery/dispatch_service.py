"""Persistent pilot dispatcher. Test mode is default; no live credentials in test mode."""
import argparse,json,os,time
from pathlib import Path
from psycopg2.extras import RealDictCursor
from outreach_recovery.delivery_repository import DeliveryRepository
from outreach_recovery.delivery_worker import DeliveryWorker
from outreach_recovery.delivery_contracts import MutationResult,MutationState,ReconciliationResult,ReconciliationState
from outreach_recovery.gmail_adapter import GmailAdapter

SENDER='sdr@cavaco.ai'
RECIPIENT='rui@cavaco.ai'

def test_mode(value=None):
    value=os.environ.get('OUTREACH_TEST_MODE','true') if value is None else value
    if value not in ('true','false'): raise ValueError('OUTREACH_TEST_MODE must be true or false')
    return value=='true'

def validate(payload):
    if payload.get('from')!=SENDER or payload.get('to')!=RECIPIENT:
        raise ValueError('Pilot address pair not allowed')
    # Validates actual MIME, including absence of extra recipients. No token call.
    GmailAdapter(mailbox_id=payload['mailbox_id'],email=SENDER,token_provider=lambda **_:None)._validate(payload)

class ScopedGmail:
    def __init__(self,provider): self.provider=provider
    def send(self,payload,**kwargs):
        try: validate(payload)
        except Exception: return MutationResult(MutationState.REJECTED)
        return self.provider.send(payload,**kwargs)
    def reconcile(self,payload,**kwargs):
        try: validate(payload)
        except Exception: return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
        return self.provider.reconcile(payload,**kwargs)

class MockGmail:
    def send(self,payload,*,operation_id,timeout_seconds):
        validate(payload)
        return MutationResult(MutationState.ACCEPTED,'mock-gmail-'+operation_id,'mock-thread-'+operation_id)

class MockCRM:
    def create_email(self,payload,*,operation_id,timeout_seconds):
        return MutationResult(MutationState.ACCEPTED,'mock-crm-'+operation_id)

def simulate_once(repository):
    # Do not claim/mutate live send intents or enqueue live CRM jobs. Locking the
    # simulation audit serializes multiple test workers via its unique key.
    with repository.connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT o.id,o.payload,o.state,d.state AS draft_state,
          d.version_snapshot,c.version_counter
          FROM outreach_pilot.delivery_operations o
          JOIN outreach_pilot.drafts d ON d.id=o.draft_id
          JOIN outreach_pilot.conversations c ON c.id=o.conversation_id
          WHERE o.kind='gmail_send' AND o.state IN ('pending','processing','reconciliation_required')
          AND NOT EXISTS(SELECT 1 FROM outreach_pilot.delivery_simulations s WHERE s.operation_id=o.id)
          ORDER BY o.id LIMIT 1""")
        row=cur.fetchone()
        if not row: return 'idle'
        gmail_id=crm_id=None
        if row['state']!='pending': outcome='reconciliation_only'
        elif row['draft_state']!='approved' or row['version_snapshot']!=row['version_counter']: outcome='blocked_stale'
        else:
            try:
                result=MockGmail().send(row['payload'],operation_id=str(row['id']),timeout_seconds=1)
                gmail_id=result.provider_id
                crm_id=MockCRM().create_email(row['payload'],operation_id=str(row['id']),timeout_seconds=1).provider_id
                outcome='simulated_completed'
            except Exception: outcome='blocked_allowlist'
        cur.execute('INSERT INTO outreach_pilot.delivery_simulations(operation_id,outcome,mock_gmail_id,mock_crm_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(row['id'],outcome,gmail_id,crm_id))
        return outcome if cur.rowcount else 'idle'

def run_cycle(repository,*,testing=True,provider=None):
    if testing: return simulate_once(repository)
    if provider is None: raise ValueError('Live provider required')
    return DeliveryWorker(repository,gmail=ScopedGmail(provider),hubspot=None).run_once('gmail_send')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot',action='store_true')
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--poll-interval',type=int,default=5)
    args=parser.parse_args()
    if not 1<=args.poll_interval<=300: parser.error('Poll interval must be 1..300 seconds')
    testing=test_mode()
    if args.pilot:
        import pgserver
        server=pgserver.get_server(Path.home()/'.local/share/cavaco-outreach/controlled-pilot/postgres',cleanup_mode='stop')
        dsn=server.get_uri()
    else: dsn=os.environ['OUTREACH_DATABASE_URL']
    repo=DeliveryRepository(dsn)
    provider=None
    if not testing:
        from outreach_recovery.gmail_auth import OAuthTokenProvider
        with repo.connection() as conn,conn.cursor() as cur:
            cur.execute('SELECT id FROM outreach_pilot.mailboxes WHERE email=%s',(SENDER,))
            row=cur.fetchone()
            if not row: raise ValueError('Pilot sender mailbox missing')
        provider=GmailAdapter(mailbox_id=row[0],email=SENDER,token_provider=OAuthTokenProvider())
    print(json.dumps({'service':'delivery','mode':'test' if testing else 'live','allowed_from':SENDER,'allowed_to':RECIPIENT}),flush=True)
    failures=0
    while True:
        try:
            outcome=run_cycle(repo,testing=testing,provider=provider)
            failures=0
            if outcome!='idle' or not args.watch: print(json.dumps({'mode':'test' if testing else 'live','outcome':outcome}),flush=True)
        except Exception:
            failures+=1
            print(json.dumps({'mode':'test' if testing else 'live','outcome':'cycle_failed','consecutive_failures':failures}),flush=True)
            if not args.watch: raise SystemExit(1)
        if not args.watch: return
        time.sleep(min(300,args.poll_interval*2**min(failures,6)))

if __name__=='__main__': main()
