import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from django.db import connection

logger = logging.getLogger(__name__)


def generate_token(length=48):
    return secrets.token_urlsafe(length)


def generate_otp(length=6):
    return ''.join(secrets.choice('0123456789') for _ in range(length))


def create_verification(email, purpose='registration', tenant_id=1, ttl_hours=24, use_otp=True):
    token = generate_otp(6) if use_otp else generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with connection.cursor() as cur:
        cur.execute('''
            INSERT INTO "EmailVerification"
            ("Email", "Token", "TenantID", "Purpose", "ExpiresAt")
            VALUES (%s, %s, %s, %s, %s)
            RETURNING "TokenID"
        ''', [email, token, tenant_id, purpose, expires_at])
    return token


def verify_token(email, token, purpose='registration'):
    with connection.cursor() as cur:
        cur.execute('''
            SELECT "TokenID", "ExpiresAt", "UsedAt" FROM "EmailVerification"
            WHERE "Email" = %s AND "Token" = %s AND "Purpose" = %s
            ORDER BY "TokenID" DESC LIMIT 1
        ''', [email, token, purpose])
        row = cur.fetchone()
        if not row:
            return False, 'Invalid token'
        token_id, expires_at, used_at = row
        if used_at:
            return False, 'Token already used'
        if expires_at < datetime.now(timezone.utc):
            return False, 'Token expired'
        cur.execute(
            'UPDATE "EmailVerification" SET "UsedAt" = NOW() WHERE "TokenID" = %s',
            [token_id],
        )
        return True, 'OK'


def send_email(to, subject, body):
    """
    Dispatch a transactional email through the configured backend.

    Pick a backend via the EMAIL_BACKEND env var:
      • console  — prints to stdout (dev default)
      • smtp     — generic SMTP (Gmail, Outlook, Brevo, any host).
                   NOTE: Render blocks outbound SMTP ports, so this
                   fails in production there — use an HTTPS API backend.
      • sendgrid — SendGrid REST API
      • resend   — Resend REST API (needs a verified domain to send
                   to anyone but yourself)
      • brevo    — Brevo (ex-Sendinblue) REST API. HTTPS, free 300/day,
                   sends from a verified address without owning a domain.
      • gmail    — Gmail API (HTTPS) as the litrix.team@gmail.com account
                   itself. No third-party provider / phone / domain / trial.
                   OAuth2 refresh token minted once via tools/gmail_oauth.py.
    """
    backend = os.getenv('EMAIL_BACKEND', 'console').lower()

    if backend == 'console':
        # Use plain print() here on purpose. The 'console' backend is
        # dev-only and its whole job is to make verification codes,
        # password-reset tokens, and invitation links visible to the
        # developer running `manage.py runserver`. Routing through
        # `logger.info` would respect Django's default WARNING root
        # level and silently swallow these messages — exactly what
        # broke this in 2026-05. Logger is reserved for real email
        # backends (SMTP/Resend/SendGrid) where structured logging
        # actually helps.
        print(f"\n{'='*60}")
        print(f"EMAIL → {to}")
        print(f"Subject: {subject}")
        print(f"{'-'*60}")
        print(body)
        print(f"{'='*60}\n", flush=True)
        return True

    if backend == 'smtp':
        return _send_via_smtp(to, subject, body)

    if backend == 'sendgrid':
        return _send_via_sendgrid(to, subject, body)

    if backend == 'resend':
        return _send_via_resend(to, subject, body)

    if backend == 'brevo':
        return _send_via_brevo(to, subject, body)

    if backend == 'gmail':
        return _send_via_gmail(to, subject, body)

    logger.warning('unknown EMAIL_BACKEND=%s', backend)
    return False


def _send_via_smtp(to, subject, body):
    """
    Generic SMTP backend — works with Gmail, Outlook, Brevo, ProtonMail
    Bridge, your university SMTP, etc.

    Required env vars:
        SMTP_HOST       e.g. smtp.gmail.com
        SMTP_PORT       587 (STARTTLS) or 465 (SSL)
        SMTP_USER       your login
        SMTP_PASSWORD   app password / SMTP token
        SMTP_FROM       optional — display address (defaults to SMTP_USER)
    """
    import smtplib
    from email.message import EmailMessage

    host = os.getenv('SMTP_HOST')
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    pwd  = os.getenv('SMTP_PASSWORD')

    if not (host and user and pwd):
        logger.error('[smtp] missing SMTP_HOST / SMTP_USER / SMTP_PASSWORD')
        return False

    # SMTP_FROM may be a bare address (litrix.team@gmail.com) OR a full
    # display form (Litrix <litrix.team@gmail.com>). Use it verbatim and
    # only add the "Litrix" display name when it's a bare address — never
    # wrap a value that already has angle brackets, or the From collapses
    # to a malformed "Litrix <Litrix>" that Gmail rejects / spam-folders.
    from_value = os.getenv('SMTP_FROM') or user
    from_header = from_value if '<' in from_value else f'Litrix <{from_value}>'

    msg = EmailMessage()
    msg['From']    = from_header
    msg['To']      = to
    msg['Subject'] = subject
    msg.set_content(body)

    try:
        # Port 465 = SMTP_SSL (implicit TLS); 587 = STARTTLS upgrade.
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        return True
    except Exception:
        logger.exception('[smtp] send failed')
        return False


def _send_via_resend(to, subject, body):
    """
    Resend (resend.com) — modern, developer-friendly transactional API.
    Required env: RESEND_API_KEY, RESEND_FROM.
    """
    api_key = os.getenv('RESEND_API_KEY')
    sender  = os.getenv('RESEND_FROM', 'Litrix <noreply@litrix.app>')
    if not api_key:
        logger.error('[resend] missing RESEND_API_KEY')
        return False
    try:
        import httpx
        r = httpx.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {api_key}'},
            json={
                'from':    sender,
                'to':      [to],
                'subject': subject,
                'text':    body,
            },
            timeout=10,
        )
        if r.status_code not in (200, 202):
            logger.error('[resend] %s %s', r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception('[resend] send failed')
        return False


def _send_via_gmail(to, subject, body):
    """
    Gmail API over HTTPS (port 443 — works on Render, which blocks SMTP).
    Sends as the litrix.team@gmail.com account itself, so there is no
    third-party provider and no phone / domain / trial gate.

    Auth is OAuth2: a long-lived refresh token (minted once with
    tools/gmail_oauth.py) is exchanged for a short-lived access token on
    each send. The refresh token stays valid as long as the OAuth consent
    screen is published "In production" (Testing status expires it in 7d).

    Required env:
        GMAIL_CLIENT_ID
        GMAIL_CLIENT_SECRET
        GMAIL_REFRESH_TOKEN
    Optional env:
        GMAIL_FROM       sender address (defaults to SMTP_USER)
        GMAIL_FROM_NAME  display name (defaults to 'Litrix')
    """
    import base64
    from email.message import EmailMessage
    from email.utils import parseaddr

    client_id     = os.getenv('GMAIL_CLIENT_ID')
    client_secret = os.getenv('GMAIL_CLIENT_SECRET')
    refresh_token = os.getenv('GMAIL_REFRESH_TOKEN')
    if not (client_id and client_secret and refresh_token):
        logger.error('[gmail] missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN')
        return False

    raw_from = os.getenv('GMAIL_FROM') or os.getenv('SMTP_USER') or ''
    name, addr = parseaddr(raw_from)
    from_name = name or os.getenv('GMAIL_FROM_NAME', 'Litrix')
    from_addr = addr or raw_from

    try:
        import httpx
        # 1) Trade the refresh token for a short-lived access token.
        tok = httpx.post(
            'https://oauth2.googleapis.com/token',
            data={
                'client_id':     client_id,
                'client_secret': client_secret,
                'refresh_token': refresh_token,
                'grant_type':    'refresh_token',
            },
            timeout=10,
        )
        if tok.status_code != 200:
            logger.error('[gmail] token refresh failed %s %s', tok.status_code, tok.text[:500])
            return False
        access_token = tok.json().get('access_token')
        if not access_token:
            logger.error('[gmail] no access_token in refresh response')
            return False

        # 2) Build the RFC-822 message and base64url-encode it (Gmail API
        #    wants the whole message in a single url-safe base64 "raw" field).
        msg = EmailMessage()
        msg['From']    = f'{from_name} <{from_addr}>' if from_name else from_addr
        msg['To']      = to
        msg['Subject'] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        # 3) Send via the Gmail API.
        r = httpx.post(
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
            headers={'Authorization': f'Bearer {access_token}'},
            json={'raw': raw},
            timeout=15,
        )
        if r.status_code not in (200, 202):
            logger.error('[gmail] send failed %s %s', r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception('[gmail] send failed')
        return False


def _send_via_brevo(to, subject, body):
    """
    Brevo (brevo.com, formerly Sendinblue) — transactional email over
    HTTPS. Picked over SMTP because Render blocks outbound SMTP ports,
    and over Resend/SendGrid because Brevo's free tier (300/day) lets us
    send from a verified sender address WITHOUT owning a custom domain —
    which Litrix doesn't have yet (still on *.vercel.app).

    Required env: BREVO_API_KEY (starts with 'xkeysib-').
    Optional env: BREVO_FROM       sender address (defaults to SMTP_USER)
                  BREVO_FROM_NAME  display name (defaults to 'Litrix')

    BREVO_FROM may be a bare address or a full 'Name <addr>' form — we
    parse it either way so we never repeat the From double-wrap bug.
    """
    from email.utils import parseaddr

    api_key = os.getenv('BREVO_API_KEY')
    if not api_key:
        logger.error('[brevo] missing BREVO_API_KEY')
        return False

    raw_from = os.getenv('BREVO_FROM') or os.getenv('SMTP_USER') or ''
    name, addr = parseaddr(raw_from)
    sender = {
        'name':  name or os.getenv('BREVO_FROM_NAME', 'Litrix'),
        'email': addr or raw_from,
    }

    try:
        import httpx
        r = httpx.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'api-key': api_key, 'accept': 'application/json'},
            json={
                'sender':      sender,
                'to':          [{'email': to}],
                'subject':     subject,
                'textContent': body,
            },
            timeout=10,
        )
        # Brevo returns 201 Created (with a messageId) on success.
        if r.status_code not in (200, 201, 202):
            logger.error('[brevo] %s %s', r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception('[brevo] send failed')
        return False


def _send_via_sendgrid(to, subject, body):
    """
    Send a single transactional email through SendGrid's v3 API.

    Why `bypass_list_management`?
        Our emails (invitations, password resets, verification codes)
        are TRANSACTIONAL — the recipient explicitly triggered them
        via an action they took or an admin took on their behalf. They
        are NOT marketing. SendGrid by default injects an unsubscribe
        footer into every send, which:
          1. Is legally unnecessary for transactional mail (CAN-SPAM,
             GDPR exempt these from opt-out requirements).
          2. Confuses users — they can't really "unsubscribe" from
             their own password reset.
        Setting bypass_list_management=true tells SendGrid to skip the
        unsubscribe footer/group for this specific send. The
        {{{unsubscribe}}} placeholders the template editor shows
        won't appear in the delivered email.
    """
    api_key = os.getenv('SENDGRID_API_KEY')
    from_email = os.getenv('SENDGRID_FROM', 'noreply@litrix.com')
    if not api_key:
        return False
    try:
        import httpx
        r = httpx.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={'Authorization': f'Bearer {api_key}'},
            json={
                'personalizations': [{'to': [{'email': to}]}],
                'from': {'email': from_email, 'name': 'Litrix'},
                'subject': subject,
                'content': [{'type': 'text/plain', 'value': body}],
                # Skip the unsubscribe injection — this is transactional.
                'mail_settings': {
                    'bypass_list_management': {'enable': True},
                },
            },
            timeout=10,
        )
        if r.status_code != 202:
            # SendGrid returns details in the body when something is off
            # (unverified sender, invalid email, etc.). Log so the
            # operator can debug from the Django logs.
            logger.error('[SendGrid] %s %s', r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception('[SendGrid] send failed')
        return False


def send_verification_email(email, token, tenant_name='Litrix'):
    body = f"""Hello,

Welcome to {tenant_name}.

Your verification code is: {token}

This code expires in 24 hours. If you didn't request this, ignore this email.

— Litrix Platform"""
    return send_email(email, f'{tenant_name} — Verify your email', body)


def send_registration_approved(email, tenant_name='Litrix'):
    body = f"""Your registration to {tenant_name} has been approved.

You can now log in at https://litrix.vercel.app

— Litrix Platform"""
    return send_email(email, f'{tenant_name} — Account approved', body)


def send_registration_rejected(email, reason, tenant_name='Litrix'):
    body = f"""Your registration request to {tenant_name} was not approved.

Reason: {reason or 'No reason provided'}

If you believe this is a mistake, please contact your department head.

— Litrix Platform"""
    return send_email(email, f'{tenant_name} — Registration update', body)


def send_password_reset(email, token):
    body = f"""You requested a password reset.

Your reset code is: {token}

This code expires in 1 hour. If you didn't request this, ignore this email.

— Litrix Platform"""
    return send_email(email, 'Litrix — Password reset', body)


def send_invitation_email(to: str, role: str, token: str, inviter_name: str):
    """
    Email the role-scoped invitation link. The base URL is configurable
    so we can swap between local dev (localhost:4200) and production
    (litrix.app, etc.) without touching the call site.
    """
    base = os.getenv('FRONTEND_BASE_URL', 'http://localhost:4200').rstrip('/')
    link = f'{base}/register?invite={token}'
    role_label = {
        'HoD':        'Head of Department',
        'Dean':       'Dean',
        'Researcher': 'Researcher',
    }.get(role, role)

    body = f"""Hello,

{inviter_name} has invited you to join Litrix as {role_label}.

Click the link below to complete your registration:
{link}

This invitation is bound to your email and expires soon. If you didn't
expect this, ignore this email.

— Litrix Platform"""

    return send_email(to, f'Litrix — You have been invited as {role_label}', body)

