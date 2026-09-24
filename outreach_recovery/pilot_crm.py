"""Preview or explicitly approve the two authorized internal pilot CRM activities."""
import argparse
import hashlib
import json
from pathlib import Path
import pgserver
from outreach_recovery.crm_repository import CRMRepository,payload_digest
from outreach_recovery.delivery_worker import DeliveryWorker
from outreach_recovery.hubspot_adapter import HubSpotAdapter
from outreach_recovery.hubspot_auth import DEFAULT_TOKEN
from outreach_recovery.gmail_auth import private_path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve",help="Exact SHA256 digest printed by preview; authorizes these two CRM logs")
    args=parser.parse_args()
    root=Path.home()/".local/share/cavaco-outreach/controlled-pilot"
    server=pgserver.get_server(root/"postgres",cleanup_mode="stop")
    repo=CRMRepository(server.get_uri())
    with repo.connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT to_regclass('outreach_pilot.crm_activity_jobs')")
        if not cur.fetchone()[0]:cur.execute(Path(__file__).with_name("009_crm_activity_jobs.sql").read_text())
        cur.execute("SELECT id,gmail_message_id,direction FROM outreach_pilot.messages WHERE gmail_message_id IN ('1a0d4cd6381d5158','1a0d4d60cf411bdd') ORDER BY received_at")
        sources=cur.fetchall()
    if len(sources)!=2:raise ValueError("Expected exactly the two controlled test messages")
    intents=[]
    for message,gmail_id,direction in sources:
        outgoing=direction=="outbound"
        job=repo.prepare(message,portal_id="47521149",contact_id="90197841728",
            sender="sdr@cavaco.ai" if outgoing else "rui@cavaco.ai",
            recipient="rui@cavaco.ai" if outgoing else "sdr@cavaco.ai",
            subject=("" if outgoing else "Re: ")+"Cavaco Outreach — Gmail delivery test")
        intents.append(job)
    digest=hashlib.sha256("".join(payload_digest(j["payload"]) for j in intents).encode()).hexdigest()
    def token():
        saved=json.loads(private_path(DEFAULT_TOKEN).read_text())
        if str(saved.get("portal_id"))!="47521149":raise ValueError("Stored portal mismatch")
        return saved["access_token"]
    adapter=HubSpotAdapter(portal_id="47521149",token_provider=token)
    preview={"portal_id":"47521149","contact_id":"90197841728","approval_digest":digest,"activities":[]}
    for job in intents:
        result=adapter.reconcile(job["payload"],operation_id=str(job["id"]),timeout_seconds=30)
        preview["activities"].append({"job_id":str(job["id"]),"state":job["state"],"payload":job["payload"],"lookup":result.state.value,"existing_id":result.provider_id})
    path=root/"hubspot-preview.json"
    path.write_text(json.dumps(preview,indent=2,ensure_ascii=False)+"\n");path.chmod(0o600)
    if args.approve:
        if args.approve!=digest:raise ValueError("Approval does not match current preview")
        if any(a["lookup"]=="lookup_failed" for a in preview["activities"]):raise ValueError("Duplicate check unresolved; no jobs approved")
        for job in intents:repo.approve(job["id"],reviewer="Rui",digest=payload_digest(job["payload"]))
        worker=DeliveryWorker(repo,gmail=None,hubspot=adapter)
        for job in intents:print(json.dumps({"job_id":str(job["id"]),"outcome":worker.run_once("hubspot_log",job["id"])}))
    else:
        print(json.dumps({"preview_path":str(path),"approval_digest":digest,"crm_records_changed":False,
              "checks":[{"job_id":a["job_id"],"state":a["state"],"lookup":a["lookup"]} for a in preview["activities"]]}))

if __name__=="__main__":main()
