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
    """Send a transactional email via the backend named in EMAIL_BACKEND.

    Options: console (dev default), smtp, sendgrid, resend, brevo, gmail.
    Render blocks outbound SMTP, so on production we use one of the HTTPS
    API backends instead of smtp.
    """
    backend = os.getenv('EMAIL_BACKEND', 'console').lower()

    if backend == 'console':
        # print() on purpose, not logger: the console backend exists to
        # show verification codes / reset tokens / invite links to whoever
        # is running manage.py runserver. Going through logger.info would
        # get swallowed by Django's default WARNING root level (it did, in
        # 2026-05). Logger stays for the real backends.
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
    """Generic SMTP backend (Gmail, Outlook, Brevo, ProtonMail Bridge, etc).

    Env: SMTP_HOST, SMTP_PORT (587 STARTTLS / 465 SSL), SMTP_USER,
    SMTP_PASSWORD, and optional SMTP_FROM (defaults to SMTP_USER).
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

    # SMTP_FROM may be a bare address or already a "Litrix <addr>" form.
    # Only add the display name when there are no angle brackets yet —
    # double-wrapping produces a malformed "Litrix <Litrix>" that Gmail
    # rejects / spam-folders.
    from_value = os.getenv('SMTP_FROM') or user
    from_header = from_value if '<' in from_value else f'Litrix <{from_value}>'

    msg = EmailMessage()
    msg['From']    = from_header
    msg['To']      = to
    msg['Subject'] = subject
    msg.set_content(body)

    try:
        # 465 = implicit TLS; 587 = STARTTLS upgrade.
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
    """Resend (resend.com) transactional API. Env: RESEND_API_KEY, RESEND_FROM."""
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
    """Gmail API over HTTPS — works on Render, which blocks SMTP. Sends as
    the litrix.team@gmail.com account itself (no third-party provider, no
    phone / domain / trial gate).

    OAuth2: the long-lived refresh token (minted once via tools/gmail_oauth.py)
    buys a short-lived access token per send. The consent screen must be
    published "In production" or Testing status expires the token after 7d.

    Env: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, plus
    optional GMAIL_FROM (defaults to SMTP_USER) and GMAIL_FROM_NAME.
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
        # Trade the refresh token for a short-lived access token.
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

        # Gmail wants the whole RFC-822 message in one url-safe base64
        # "raw" field.
        msg = EmailMessage()
        msg['From']    = f'{from_name} <{from_addr}>' if from_name else from_addr
        msg['To']      = to
        msg['Subject'] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

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
    """Brevo (ex-Sendinblue) transactional email over HTTPS. Chosen over SMTP
    (Render blocks it) and over Resend/SendGrid because Brevo's free tier
    (300/day) sends from a verified address without owning a custom domain,
    which Litrix doesn't have yet (still on *.vercel.app).

    Env: BREVO_API_KEY (starts with 'xkeysib-'), plus optional BREVO_FROM
    (defaults to SMTP_USER) and BREVO_FROM_NAME. BREVO_FROM is parsed
    whether it's bare or "Name <addr>", to avoid the From double-wrap bug.
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
    """Send one transactional email through SendGrid's v3 API.

    bypass_list_management=true skips the unsubscribe footer SendGrid
    injects by default. Our mail (invites, resets, codes) is transactional,
    not marketing — the footer is legally unnecessary (CAN-SPAM/GDPR exempt)
    and confusing (you can't unsubscribe from your own password reset).
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
                # Skip unsubscribe injection — transactional mail.
                'mail_settings': {
                    'bypass_list_management': {'enable': True},
                },
            },
            timeout=10,
        )
        if r.status_code != 202:
            # SendGrid puts the failure reason (unverified sender, bad
            # email, etc.) in the body — log it so we can debug.
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
    """Email the role-scoped invitation link. FRONTEND_BASE_URL lets us swap
    between local dev and production without touching the call site."""
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

