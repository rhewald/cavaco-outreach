"""Validated display links; CRM content never controls arbitrary URL schemes."""
from urllib.parse import urlsplit, urlunsplit

def linkedin_url(value):
    value=(value or '').strip()
    if not value: return ''
    if '://' not in value: value='https://'+value
    try:
        u=urlsplit(value)
        if u.scheme not in ('http','https') or u.username or u.password or u.port or any(c.isspace() for c in value): return ''
        if (u.hostname or '').lower() not in ('linkedin.com','www.linkedin.com') or not u.path.startswith('/in/'): return ''
        return urlunsplit(('https','www.linkedin.com',u.path,'',''))
    except ValueError: return ''

def contact_fields(props):
    link=next((url for key in ('hs_linkedin_url','sl_contact_linkedin_url','linkedin_account','hs_linkedinid') if (url:=linkedin_url(props.get(key)))),'')
    phones=[]
    for key,label in [('phone','Phone'),('mobilephone','Mobile'),('hs_whatsapp_phone_number','WhatsApp'),('sl_phone_numbers','Other numbers')]:
        value=(props.get(key) or '').strip()
        if value and value not in [p['value'] for p in phones]: phones.append({'label':label,'value':value})
    return link,phones

def decorate(contact):
    contact['linkedin_url']=linkedin_url(contact.get('linkedin_url'))
    portal=str(contact.get('portal_id') or '')
    cid=str(contact.get('contact_id') or '')
    contact['hubspot_url']=f'https://app.hubspot.com/contacts/{portal}/record/0-1/{cid}?utm_source=cavaco_outreach&utm_medium=ai_agent' if portal.isdigit() and cid.isdigit() else ''
    return contact
