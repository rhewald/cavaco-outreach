"""Process CRM jobs from both queues; never claims a Gmail send operation."""
import argparse
import json
import os
import time
from pathlib import Path
from outreach_recovery.crm_repository import CRMRepository
from outreach_recovery.delivery_repository import DeliveryRepository
from outreach_recovery.delivery_worker import DeliveryWorker
from outreach_recovery.hubspot_adapter import HubSpotAdapter
from outreach_recovery.hubspot_auth import DEFAULT_TOKEN
from outreach_recovery.gmail_auth import private_path


class PortalQueue:
    def __init__(self, repository, portal_id):
        self.repository=repository
        self.portal_id=str(portal_id)
    def claim(self, *, kind, **kwargs):
        if kind!='hubspot_log':raise ValueError('CRM only')
        return self.repository.claim(kind=kind,portal_id=self.portal_id,**kwargs)
    def __getattr__(self,name):return getattr(self.repository,name)


def run_cycle(dsn, portal_id, adapter):
    # Service one job from each lane so neither can starve the other.
    outcomes=[]
    for repository in (DeliveryRepository(dsn),CRMRepository(dsn)):
        worker=DeliveryWorker(PortalQueue(repository,portal_id),gmail=None,hubspot=adapter)
        outcomes.append(worker.run_once('hubspot_log'))
    return outcomes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portal',required=True,type=int)
    parser.add_argument('--pilot',action='store_true',help='Use the persistent local pilot database')
    parser.add_argument('--watch',action='store_true',help='Run until interrupted, polling every five seconds')
    args=parser.parse_args()
    def token():
        saved=json.loads(private_path(DEFAULT_TOKEN).read_text())
        if str(saved.get('portal_id'))!=str(args.portal):raise ValueError('Credential account mismatch')
        return saved['access_token']
    token() # Configuration failure before any job is claimed.
    if args.pilot:
        import pgserver
        server=pgserver.get_server(Path.home()/'.local/share/cavaco-outreach/controlled-pilot/postgres',cleanup_mode='stop')
        dsn=server.get_uri()
    else:
        dsn=os.environ['OUTREACH_DATABASE_URL']
    adapter=HubSpotAdapter(portal_id=args.portal,token_provider=token)
    while True:
        outcomes=run_cycle(dsn,args.portal,adapter)
        if outcomes!=['idle','idle'] or not args.watch:print(json.dumps({'crm_outcomes':outcomes}),flush=True)
        if not args.watch:return
        time.sleep(5)

if __name__=='__main__':main()
