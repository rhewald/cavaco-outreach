"""Ingest the authorized pilot reply; optionally generate a review-only draft."""
import argparse,json,os,getpass
from pathlib import Path
from uuid import uuid5,NAMESPACE_URL
import pgserver
from psycopg2.extras import Json
from outreach_recovery.generation import GenerationRepository,GenerationWorker,ProcessGenerator
from outreach_recovery.gmail_adapter import GmailAdapter
from outreach_recovery.gmail_auth import OAuthTokenProvider
from outreach_recovery.gmail_inbound import verify_accepted,fetch_message,parse_reply,ingest_message
from outreach_recovery.smoke_openai import SingleJobRepository

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt-key',action='store_true')
    parser.add_argument('--review',action='store_true')
    args=parser.parse_args()
    data=Path.home()/'.local/share/cavaco-outreach/controlled-pilot'
    server=pgserver.get_server(data/'postgres',cleanup_mode='stop')
    dsn=server.get_uri();repo=GenerationRepository(dsn)
    def sql(q,params=None):
        with repo.connection() as conn,conn.cursor() as c:
            c.execute(q,params)
            return c.fetchall() if c.description else []
    if not sql("SELECT to_regclass('outreach_pilot.gmail_sent_observations')")[0][0]:
        sql(Path(__file__).with_name('008_provider_observations.sql').read_text())
    op='23dca280-ba44-4cd6-8bea-1cdcf4790904'
    row=sql("SELECT mailbox_id,conversation_id,payload,provider_id,provider_thread_id FROM outreach_pilot.delivery_operations WHERE id=%s AND state='completed'",(op,))[0]
    mailbox,conv,payload,provider_id,thread=row
    adapter=GmailAdapter(mailbox_id=mailbox,email='sdr@cavaco.ai',token_provider=OAuthTokenProvider())
    observed=verify_accepted(adapter,payload,provider_id,thread)
    sql('INSERT INTO outreach_pilot.gmail_sent_observations(operation_id,provider_message_id,provider_thread_id,actual_mime_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(op,provider_id,thread,observed))
    raw=fetch_message(adapter,'1a0d4d60cf411bdd')
    parsed=parse_reply(raw,expected_sender='rui@cavaco.ai',mailbox_email='sdr@cavaco.ai',thread_id=thread)
    if observed not in parsed['reply_ids']:raise ValueError('Reply does not reference the verified sent message')
    parsed['mailbox_id']=str(mailbox)
    result=ingest_message(repo,parsed)
    job=sql('SELECT id,state FROM outreach_pilot.reply_jobs WHERE triggering_message_id=(SELECT id FROM outreach_pilot.messages WHERE mailbox_id=%s AND gmail_message_id=%s)',(mailbox,parsed['gmail_message_id']))[0]
    # These are test-only constraints derived from the approved delivery test,
    # not a marketing/product profile or authority to make product claims.
    if not sql('SELECT 1 FROM outreach_pilot.conversation_contexts WHERE conversation_id=%s',(conv,)):
        facts=dict(schema_version=1,company_name='Cavaco Outreach',seller_name='Cavaco Outreach',product_description='Controlled internal email delivery and reply-threading test only.',conversation_purpose='Briefly acknowledge receipt of the internal test reply. Do not pitch products or propose a meeting.',meeting_policy='Do not offer or book meetings for this internal test.',allowed_claims=[],pricing={'mode':'undisclosed'})
        seller=repo.create_seller_profile(facts,'Controlled test scope authorized by Rui in chat')
        sql('INSERT INTO outreach_pilot.conversation_contexts(conversation_id,seller_profile_id,prospect_research) VALUES(%s,%s,%s)',(conv,seller,'Internal test recipient; no prospect research.'))
    print(json.dumps({'sent_verified':True,'reply_ingestion':result[0],'job_id':str(job[0]),'job_state':job[1],'email_sent':False}),flush=True)
    if args.prompt_key:
        os.environ['OPENAI_API_KEY']=getpass.getpass('OpenAI API key (hidden): ')
        os.environ['OUTREACH_OPENAI_MODEL']='gpt-4o-mini'
        worker=GenerationWorker(SingleJobRepository(dsn,job[0]),ProcessGenerator('outreach_recovery.openai_adapter:generate',60))
        print(json.dumps({'generation_outcome':worker.run_once(),'email_sent':False}),flush=True)
    drafts=sql('SELECT id,state FROM outreach_pilot.drafts WHERE triggering_message_id=(SELECT triggering_message_id FROM outreach_pilot.reply_jobs WHERE id=%s)',(job[0],))
    for d in drafts:print(json.dumps({'draft_id':str(d[0]),'state':d[1]}),flush=True)
    if args.review:
        import uvicorn
        from outreach_recovery.review_app import create_app
        from outreach_recovery.review_repository import ReviewRepository
        print('Open http://127.0.0.1:8766/reviews',flush=True)
        uvicorn.run(create_app(ReviewRepository(dsn),'Rui',8766),host='127.0.0.1',port=8766,proxy_headers=False,access_log=False)

if __name__=='__main__':main()
