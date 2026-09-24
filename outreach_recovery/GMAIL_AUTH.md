# Gmail sign-in for Cavaco Outreach

Selected pilot mailbox: `sdr@cavaco.ai`. No mailbox has been connected or test email
sent by this implementation. This must be the account's primary Gmail address;
an alias or Google Group is not sufficient for the current adapter.

## Google setup (account owner)

1. In a Google Cloud project, enable the Gmail API.
2. Configure Google Auth Platform branding/audience. Use Internal if appropriate
   and available for your Workspace organization; otherwise configure testing
   access for the intended user. Workspace administrator approval may be required.
3. Create an OAuth client of type **Desktop app** and download its client JSON.
   Keep it outside the repository. Do not paste the JSON or tokens into chat.
4. Run the command below locally and sign in as `sdr@cavaco.ai`. Grant the displayed
   Gmail read and send scopes. The setup command itself only reads the profile;
   granting send scope does not send an email.

Official setup: https://developers.google.com/workspace/gmail/api/quickstart/python
Desktop/PKCE reference: https://developers.google.com/identity/protocols/oauth2/native-app

## Local commands

Use Python 3.12+ with a supported OpenSSL runtime. The current machine has an isolated
ready environment at `/private/tmp/cavaco-outreach-py312`; temporary directories may
be cleaned by macOS. To recreate elsewhere, create a Python 3.12 venv and install
`outreach_recovery/requirements.txt`.

```sh
cd /Users/ruihewald/Documents/cavaco-outreach
/private/tmp/cavaco-outreach-py312/bin/python -m outreach_recovery.gmail_auth connect \
  --email sdr@cavaco.ai --client-json /absolute/path/to/downloaded-client.json
```

A browser opens for consent with PKCE and OAuth state validation. The callback
listener binds to 127.0.0.1 on a random port and waits up to 180 seconds. The account
profile must match the expected email before credentials are saved. Credentials
are stored in `~/.config/cavaco-outreach/gmail-token.json` (0600, private directory
0700), outside Git. This is permission-protected local JSON, not encrypted Keychain
storage. Protect local account access. No refresh token or access token is printed.
Use separate token paths for different mailboxes and re-run connect to change users.

Read-only verification (refreshes OAuth token if needed, never sends email):

```sh
/private/tmp/cavaco-outreach-py312/bin/python -m outreach_recovery.gmail_auth verify \
  --email sdr@cavaco.ai
```

Successful output contains the verified mailbox, `verified: true`, and
`email_sent: false`. This does not prove sending permissions or inbox placement.
The worker uses `OAuthTokenProvider(token_path)` as the GmailAdapter token provider.
Automatic refresh never launches browser consent or retries a Gmail send. Refresh
failure stops that send intent before mutation. Provider calls enforce remaining
socket timeout budgets, not a hard process watchdog. One process per mailbox is
recommended for this local pilot; refresh locking is process-local.

## Next gate

After read-only verification: choose one test recipient you control, review the
exact email, and explicitly approve the controlled send. Gmail reconciliation and
HubSpot logging still require live verification before real outreach.
