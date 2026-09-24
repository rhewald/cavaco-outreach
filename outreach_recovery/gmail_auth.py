"""Local desktop OAuth onboarding and read-only Gmail verification. Never sends mail."""
import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

import httpx

SCOPES = ('https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.send')
TOKEN_URI = 'https://oauth2.googleapis.com/token'
DEFAULT_TOKEN = Path.home()/'.config/cavaco-outreach/gmail-token.json'


class AuthenticationError(RuntimeError):
    """Sanitized credential/configuration failure."""


def private_path(path):
    path=Path(path).expanduser().absolute()
    if path.is_symlink(): raise AuthenticationError('Token path must not be a symlink')
    resolved=path.resolve()
    if any((parent/'.git').exists() for parent in (resolved.parent,*resolved.parents)):
        raise AuthenticationError('Store OAuth credentials outside Git repositories')
    if path.exists():
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise AuthenticationError('Token file must be owned by you with permissions 0600')
    return resolved


def save_credentials(path,credentials):
    path=private_path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Do not silently change an existing shared directory's permissions.
    if path.parent.stat().st_uid!=os.getuid() or stat.S_IMODE(path.parent.stat().st_mode)&0o077:
        raise AuthenticationError('Use a private token directory with permissions 0700')
    fd,temp=tempfile.mkstemp(prefix='.gmail-token-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(credentials.to_json()); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def verify_mailbox(token,expected_email,*,timeout_seconds=15,transport=None):
    """Only GET profile. No email bodies or messages are fetched by this command."""
    try:
        with httpx.Client(transport=transport or httpx.HTTPTransport(retries=0),trust_env=False,follow_redirects=False) as client:
            response=client.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                headers={'Authorization':'Bearer '+token},timeout=timeout_seconds)
        if response.status_code!=200: raise ValueError()
        email=response.json().get('emailAddress')
        if not isinstance(email,str) or email.casefold()!=expected_email.casefold():
            raise AuthenticationError('Signed-in mailbox does not match the expected address')
        return email
    except AuthenticationError: raise
    except Exception: raise AuthenticationError('Mailbox verification failed') from None


class OAuthTokenProvider:
    """Callable credential source for GmailAdapter; refreshes locally, never prompts."""
    def __init__(self,path=DEFAULT_TOKEN):
        self.path=private_path(path)
        self.lock=threading.Lock()

    def __call__(self,*,timeout_seconds):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        import requests
        deadline=time.monotonic()+timeout_seconds
        if timeout_seconds<=0 or not self.lock.acquire(timeout=timeout_seconds):
            raise AuthenticationError('Credential budget exhausted')
        try:
            private_path(self.path)
            data=json.loads(self.path.read_text())
            if data.get('token_uri')!=TOKEN_URI or not set(SCOPES)<=set(data.get('scopes',[])):
                raise AuthenticationError('OAuth token scope or endpoint mismatch; reconnect')
            creds=Credentials.from_authorized_user_info(data,SCOPES)
            if not creds.valid:
                if not creds.refresh_token: raise AuthenticationError('Reconnect the Gmail account')
                with requests.Session() as session:
                    session.trust_env=False
                    request=Request(session=session)
                    def bounded_request(*args,**kwargs):
                        remaining=deadline-time.monotonic()
                        if remaining<=0: raise AuthenticationError('Credential budget exhausted')
                        kwargs['timeout']=remaining
                        return request(*args,**kwargs)
                    creds.refresh(bounded_request)
                if not creds.has_scopes(SCOPES): raise AuthenticationError('Required permissions missing')
                save_credentials(self.path,creds)
            if time.monotonic()>=deadline or not creds.token:
                raise AuthenticationError('Credential budget exhausted or token unavailable')
            return creds.token
        except AuthenticationError: raise
        except Exception: raise AuthenticationError('Gmail credentials unavailable; reconnect locally') from None
        finally: self.lock.release()


def connect(client_json,token_path,expected_email):
    from google_auth_oauthlib.flow import InstalledAppFlow
    private_path(token_path)
    try:
        config=json.loads(Path(client_json).expanduser().read_text())
        installed=config.get('installed',{})
        if installed.get('auth_uri')!='https://accounts.google.com/o/oauth2/auth' or installed.get('token_uri')!=TOKEN_URI:
            raise AuthenticationError('Use a Google OAuth Desktop app client JSON')
        flow=InstalledAppFlow.from_client_config(config,SCOPES,autogenerate_code_verifier=True)
        creds=flow.run_local_server(host='127.0.0.1',port=0,timeout_seconds=180,
            authorization_prompt_message='Complete Google authorization in your browser.',
            success_message='Authorization received. Return to the terminal for mailbox verification.',
            open_browser=True,access_type='offline',prompt='consent',login_hint=expected_email)
        granted=creds.granted_scopes if creds.granted_scopes is not None else creds.scopes
        if not set(SCOPES)<=set(granted or ()) or not creds.refresh_token:
            raise AuthenticationError('Grant Gmail read/send permissions and offline access')
        email=verify_mailbox(creds.token,expected_email)
        save_credentials(token_path,creds)
        return email
    except AuthenticationError: raise
    except Exception: raise AuthenticationError('Google authorization did not complete; retry locally') from None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('connect','verify'))
    parser.add_argument('--email',required=True,help='Expected primary Google Workspace/Gmail address')
    parser.add_argument('--client-json',help='Downloaded Google OAuth Desktop app client JSON')
    parser.add_argument('--token-file',type=Path,default=DEFAULT_TOKEN)
    args=parser.parse_args(argv)
    if args.command=='connect' and not args.client_json: parser.error('connect requires --client-json')
    try:
        if args.command=='connect': email=connect(args.client_json,args.token_file,args.email)
        else: email=verify_mailbox(OAuthTokenProvider(args.token_file)(timeout_seconds=20),args.email)
        print(json.dumps({'mailbox':email,'verified':True,'email_sent':False}))
        return 0
    except AuthenticationError as error:
        print(str(error))
        return 1


if __name__=='__main__': raise SystemExit(main())
