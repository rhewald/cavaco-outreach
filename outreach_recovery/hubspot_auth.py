"""Hidden local private-app token setup; verifies access without CRM mutations."""
import argparse
import getpass
import json
from pathlib import Path
import sys
import httpx
from outreach_recovery.gmail_auth import save_credentials, private_path

DEFAULT_TOKEN = Path.home()/'.config/cavaco-outreach/hubspot-token.json'
REQUIRED = {'crm.objects.contacts.read', 'crm.objects.contacts.write', 'sales-email-read'}


def verify(token, portal, transport=None):
    if not token or any(c.isspace() for c in token):
        raise ValueError('A nonempty token without whitespace is required')
    with httpx.Client(transport=transport or httpx.HTTPTransport(retries=0),
                      trust_env=False, follow_redirects=False, timeout=20) as client:
        response = client.post('https://api.hubapi.com/oauth/v2/private-apps/get/access-token-info',
                               json={'tokenKey': token})
        scopes_verified = response.status_code == 200
        if scopes_verified:
            info = response.json()
            if str(info.get('hubId')) != str(portal):
                raise ValueError('Token belongs to a different HubSpot account')
            if REQUIRED - set(info.get('scopes', [])):
                raise ValueError('Required scope missing; recheck the app scopes')
        elif response.status_code == 404:
            # An introspection failure alone does not establish token invalidity.
            account = client.get('https://api.hubapi.com/account-info/v3/details',
                                 headers={'Authorization': 'Bearer '+token})
            if account.status_code != 200:
                raise ValueError('Account verification failed (HTTP %s); confirm you copied the access token from Auth' % account.status_code)
            if str(account.json().get('portalId')) != str(portal):
                raise ValueError('Token belongs to a different HubSpot account')
            info = {}
        else:
            raise ValueError('Token verification failed (HTTP %s)' % response.status_code)
        contacts = client.get('https://api.hubapi.com/crm/v3/objects/contacts',
                              headers={'Authorization': 'Bearer '+token},
                              params={'limit': 1, 'properties': 'hs_object_id'})
        if contacts.status_code != 200:
            raise ValueError('Contact read check failed (HTTP %s)' % contacts.status_code)
        response = client.get('https://api.hubapi.com/crm/v3/objects/emails',
                              headers={'Authorization': 'Bearer '+token},
                              params={'limit': 1, 'properties': 'hs_email_subject'})
        if response.status_code != 200:
            raise ValueError('Email activity read check failed (HTTP %s)' % response.status_code)
        return {'portal_id': str(portal), 'app_id': info.get('appId'),
                'required_scopes_verified': scopes_verified, 'contact_read_verified': True,
                'email_read_verified': True, 'write_access_tested': False}


class Credential:
    def __init__(self, token, result):
        self.data = dict(result, access_token=token)
    def to_json(self):
        return json.dumps(self.data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portal', required=True, type=int)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.verify_only:
            token = json.loads(private_path(DEFAULT_TOKEN).read_text())['access_token']
        else:
            if not sys.stdin.isatty():
                raise ValueError('Run in an interactive Terminal for hidden key entry')
            token = getpass.getpass('HubSpot app token (hidden): ').strip()
        result = verify(token, args.portal)
        if not args.verify_only:
            save_credentials(DEFAULT_TOKEN, Credential(token, result))
        print(json.dumps(dict(result, crm_records_changed=False)))
        return 0
    except ValueError as error:
        # JSON/parser errors can include input; print only our explicit safe errors.
        safe = str(error) if type(error) is ValueError else 'Invalid provider response'
        print(safe)
        return 1
    except Exception:
        print('HubSpot connection or local credential storage failed; no CRM records changed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
