"""One synthetic job, disposable PostgreSQL, one generation attempt; never sends mail."""
import argparse
import getpass
import json
import logging
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from outreach_recovery.generation import (GenerationRepository, GenerationWorker,
    ProcessGenerator, ModelConfigurationError)


class SingleJobRepository(GenerationRepository):
    def __init__(self, dsn, job_id):
        super().__init__(dsn)
        self.job_id = job_id

    def claim(self, lease_seconds=120, job_id=None):
        return super().claim(lease_seconds, self.job_id)


def run_synthetic_job(dsn, generator):
    """Fixture helper. Caller must supply a disposable database with migrations."""
    repo = GenerationRepository(dsn)
    root = Path(__file__).resolve().parent
    seller = repo.create_seller_profile(json.loads((root/"seller_profile.example.json").read_text()),
                                        "synthetic-smoke-test")
    mailbox,conversation = str(uuid4()),str(uuid4())
    with repo.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO outreach_pilot.mailboxes(id,email) VALUES (%s,%s)",
                    (mailbox,mailbox+"@example.test"))
        cur.execute("INSERT INTO outreach_pilot.conversations(id,mailbox_id,gmail_thread_id) VALUES (%s,%s,'synthetic-thread')",
                    (conversation,mailbox))
        cur.execute("INSERT INTO outreach_pilot.conversation_contexts(conversation_id,seller_profile_id,prospect_research) VALUES (%s,%s,%s)",
                    (conversation,seller,"Synthetic company evaluating an example product. No real person or business."))
        cur.execute("SELECT * FROM outreach_pilot.ingest_reply(%s,'synthetic-reply','synthetic-thread',NULL,ARRAY[]::text[],%s,now())",
                    (mailbox,"Thanks. Could you describe the product and whether a meeting is possible?"))
        message_id=cur.fetchone()[1]
        cur.execute("SELECT id FROM outreach_pilot.reply_jobs WHERE triggering_message_id=%s",(message_id,))
        job_id=cur.fetchone()[0]
    worker = GenerationWorker(SingleJobRepository(dsn,job_id),generator)
    outcome=worker.run_once()
    with repo.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT state,attempts FROM outreach_pilot.reply_jobs WHERE id=%s",(job_id,))
        state,attempts=cur.fetchone()
        cur.execute("SELECT state,body FROM outreach_pilot.drafts WHERE triggering_message_id=%s",(message_id,))
        draft=cur.fetchone()
    success=outcome=="drafted" and state=="completed" and attempts==1 and draft and draft[0]=="pending_review"
    return {"success":bool(success),"job_id":str(job_id),"outcome":outcome,"job_state":state,
            "attempts":attempts,"draft_state":draft[0] if draft else None,
            "body":draft[1] if draft else None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live",action="store_true",required=True,help="Makes one potentially billable OpenAI request")
    parser.add_argument("--model",required=True)
    parser.add_argument("--prompt-key",action="store_true",help="Read API key privately from your terminal, not command history")
    args=parser.parse_args()
    os.environ["OUTREACH_OPENAI_MODEL"]=args.model
    if args.prompt_key:
        os.environ["OPENAI_API_KEY"]=getpass.getpass("OpenAI API key (hidden): ")
    generator=ProcessGenerator("outreach_recovery.openai_adapter:generate",60)
    try:
        generator.preflight()
    except ModelConfigurationError as exc:
        print("Smoke test blocked: "+exc.code)
        return 2
    # Live tests never use OUTREACH_DATABASE_URL or a user's operational database.
    import pgserver
    import psycopg2
    root=Path(__file__).resolve().parent
    server=pgserver.get_server(Path(tempfile.mkdtemp(prefix="outreach-smoke-")),cleanup_mode="delete")
    dsn=server.get_uri()
    conn=psycopg2.connect(dsn)
    try:
        with conn,conn.cursor() as cur:
            for name in ("inbound.sql","002_ingestion_concurrency.sql","003_draft_generation.sql","004_seller_validation.sql"):
                cur.execute((root/name).read_text())
    finally:
        conn.close()
    logging.basicConfig(level=logging.INFO)
    try:
        result=run_synthetic_job(dsn,generator)
    except ModelConfigurationError as exc:
        print("Smoke test stopped: "+exc.code)
        return 2
    body=result.pop("body")
    print(json.dumps(result,sort_keys=True))
    if body:
        print("\nSynthetic draft for human review (not sent):\n"+body)
    return 0 if result["success"] else 1


if __name__=="__main__":
    raise SystemExit(main())
