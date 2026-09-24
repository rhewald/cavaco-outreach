"""Monitor enrolled Gmail threads and generate review-only drafts; never sends mail."""
import argparse,json,os,time
from pathlib import Path
from outreach_recovery.generation import GenerationRepository,GenerationWorker,ProcessGenerator,ModelConfigurationError
from outreach_recovery.gmail_adapter import GmailAdapter
from outreach_recovery.gmail_auth import OAuthTokenProvider,private_path
from outreach_recovery.gmail_monitor import MonitorRepository,GmailMonitor,GmailHistorySource
from outreach_recovery.openai_local_auth import DEFAULT_KEY


class EnrolledGenerationRepository(GenerationRepository):
    def __init__(self,dsn,mailbox_id):super().__init__(dsn);self.mailbox_id=mailbox_id
    def claim(self,lease_seconds=120):
        with self.connection() as c,c.cursor() as q:
            q.execute("""SELECT j.id FROM outreach_pilot.reply_jobs j
                JOIN outreach_pilot.conversations c ON c.id=j.conversation_id
                JOIN outreach_pilot.crm_conversation_routes r ON r.conversation_id=c.id
                JOIN outreach_pilot.conversation_contexts x ON x.conversation_id=c.id
                WHERE c.mailbox_id=%s AND ((j.state='pending' AND j.next_attempt_at<=clock_timestamp())
                   OR (j.state='processing' AND j.lease_until<=clock_timestamp()))
                ORDER BY j.next_attempt_at,j.id LIMIT 10""",(self.mailbox_id,))
            ids=[row[0] for row in q.fetchall()]
        for identifier in ids:
            job=super().claim(lease_seconds,identifier)
            if job:return job
        return None


def load_generation_key():
    if os.environ.get('OPENAI_API_KEY'):return True
    if not DEFAULT_KEY.exists():return False
    data=json.loads(private_path(DEFAULT_KEY).read_text())
    value=data.get('api_key')
    if not isinstance(value,str) or not value or any(c.isspace() for c in value):return False
    os.environ['OPENAI_API_KEY']=value
    os.environ['OUTREACH_OPENAI_MODEL']=data.get('model','gpt-4o-mini')
    return True


def generate_once(repository):
    try:
        if not load_generation_key():return 'needs_openai_key'
        return GenerationWorker(repository,ProcessGenerator('outreach_recovery.openai_adapter:generate',60)).run_once()
    except ModelConfigurationError:return 'generation_configuration_error'
    except Exception:return 'generation_failed'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mailbox',required=True)
    parser.add_argument('--pilot',action='store_true')
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    if args.pilot:
        import pgserver
        server=pgserver.get_server(Path.home()/'.local/share/cavaco-outreach/controlled-pilot/postgres',cleanup_mode='stop')
        dsn=server.get_uri()
    else:dsn=os.environ['OUTREACH_DATABASE_URL']
    with GenerationRepository(dsn).connection() as c,c.cursor() as q:
        q.execute('SELECT id FROM outreach_pilot.mailboxes WHERE lower(email)=lower(%s)',(args.mailbox,))
        row=q.fetchone()
        if not row:raise ValueError('Mailbox must be enrolled first')
        mailbox_id=str(row[0])
    adapter=GmailAdapter(mailbox_id=mailbox_id,email=args.mailbox,token_provider=OAuthTokenProvider())
    monitor=GmailMonitor(MonitorRepository(dsn,mailbox_id),GmailHistorySource(adapter))
    generation=EnrolledGenerationRepository(dsn,mailbox_id)
    previous=None
    while True:
        status={'inbox':monitor.run_once(),'generation':generate_once(generation)}
        if status!=previous or status['generation'] not in ('idle','needs_openai_key') or not args.watch:
            print(json.dumps(status),flush=True)
        previous=status
        if not args.watch:return
        time.sleep(30)

if __name__=='__main__':main()
