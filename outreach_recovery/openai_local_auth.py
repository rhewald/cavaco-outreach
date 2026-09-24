"""Store a generation key locally using hidden input, outside Git and launchd plists."""
import getpass,json,sys
from pathlib import Path
from outreach_recovery.gmail_auth import save_credentials
DEFAULT_KEY=Path.home()/'.config/cavaco-outreach/openai-token.json'

def main():
    if not sys.stdin.isatty():raise SystemExit('Run this command in an interactive Terminal')
    value=getpass.getpass('OpenAI API key (hidden): ').strip()
    if not value or any(c.isspace() for c in value):raise SystemExit('A nonempty key without whitespace is required')
    class Credential:
        def to_json(self):return json.dumps({'api_key':value,'model':'gpt-4o-mini'})
    save_credentials(DEFAULT_KEY,Credential())
    print('Saved securely. The inbox service will load it on its next cycle. No request was sent.')

if __name__=='__main__':main()
