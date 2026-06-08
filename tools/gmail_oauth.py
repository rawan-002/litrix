"""
One-time helper: mint a Gmail API refresh token for litrix.team@gmail.com
so the backend can send transactional mail via the Gmail API (HTTPS) on
Render, which blocks outbound SMTP.

Run this LOCALLY (not on the server), once:

    pip install google-auth-oauthlib
    python tools/gmail_oauth.py path/to/client_secret.json

It opens a browser → sign in as litrix.team@gmail.com → Allow. (You'll see
an "unverified app" screen for your own app — click Advanced → Go to ...
(unsafe); it's your own project, it's safe.) The script then prints the
env vars to paste into Render → Environment.

The refresh token stays valid as long as the OAuth consent screen is
published "In production". In "Testing" status Google expires it after
7 days, so publish to production before relying on it.
"""
import json
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dependency. Run:  pip install google-auth-oauthlib")
    sys.exit(1)

# Least-privilege: send-only. We never read or modify the mailbox.
SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/gmail_oauth.py <client_secret.json>")
        sys.exit(1)

    secrets_path = sys.argv[1]
    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
    # prompt='consent' forces Google to return a refresh_token even if this
    # account already authorized the app before.
    creds = flow.run_local_server(port=0, prompt='consent')

    with open(secrets_path, encoding='utf-8') as f:
        info = json.load(f)
    client = info.get('installed') or info.get('web') or {}

    print("\n" + "=" * 62)
    print("Paste these into Render -> Environment:")
    print("=" * 62)
    print("EMAIL_BACKEND        = gmail")
    print("GMAIL_CLIENT_ID      =", client.get('client_id'))
    print("GMAIL_CLIENT_SECRET  =", client.get('client_secret'))
    print("GMAIL_REFRESH_TOKEN  =", creds.refresh_token)
    print("GMAIL_FROM           = litrix.team@gmail.com")
    print("=" * 62)

    if not creds.refresh_token:
        print("\nWARNING: no refresh_token returned. Revoke prior access at")
        print("https://myaccount.google.com/permissions then re-run this.")


if __name__ == '__main__':
    main()
