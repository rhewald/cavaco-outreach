"""Refresh display-only names/company from enrolled HubSpot contacts. No CRM writes."""
import json
from pathlib import Path
import httpx
from outreach_recovery.review_repository import ReviewRepository

def refresh(dsn):
    repo=ReviewRepository(dsn)
    credentials=json.loads((Path.home()/'.config/cavaco-outreach/hubspot-token.json').read_text())
    with repo.connection() as conn,conn.cursor() as cur:
        cur.execute('SELECT conversation_id,portal_id,contact_id,contact_email FROM outreach_pilot.crm_conversation_routes')
        routes=cur.fetchall()
    count=0
    with httpx.Client(headers={'Authorization':'Bearer '+credentials['access_token']},timeout=20,trust_env=False,follow_redirects=False) as client:
        for conversation,portal,contact,email in routes:
            if str(portal)!=str(credentials['portal_id']): continue
            response=client.get('https://api.hubapi.com/crm/v3/objects/contacts/'+contact,params={'properties':'firstname,lastname,company,email','associations':'companies'})
            response.raise_for_status()
            props=response.json().get('properties',{})
            if (props.get('email') or '').casefold()!=email.casefold(): continue
            company=props.get('company') or ''
            associations=response.json().get('associations',{}).get('companies',{}).get('results',[])
            if not company and len(associations)==1:
                company_response=client.get('https://api.hubapi.com/crm/v3/objects/companies/'+str(associations[0]['id']),params={'properties':'name'})
                if company_response.status_code==200: company=company_response.json().get('properties',{}).get('name') or ''
            name=' '.join(v for v in (props.get('firstname'),props.get('lastname')) if v)
            with repo.connection() as conn,conn.cursor() as cur:
                cur.execute("INSERT INTO outreach_pilot.review_contacts(conversation_id,display_name,company_name,source) VALUES(%s,%s,%s,'HubSpot contact') ON CONFLICT(conversation_id) DO UPDATE SET display_name=excluded.display_name,company_name=excluded.company_name,source=excluded.source,updated_at=now()",(conversation,name,company))
            count+=1
    return count

if __name__=='__main__':
    import os
    import pgserver
    dsn=os.environ.get('OUTREACH_DATABASE_URL')
    if not dsn:
        server=pgserver.get_server(Path.home()/'.local/share/cavaco-outreach/controlled-pilot/postgres',cleanup_mode='stop')
        dsn=server.get_uri()
    print('Contact display records refreshed:',refresh(dsn))
