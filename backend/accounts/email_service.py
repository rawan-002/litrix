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
      • smtp     — generic SMTP (Gmail, Outlook, Brevo, any host)
      • sendgrid — SendGrid REST API
      • resend   — Resend REST API (modern SendGrid alternative)
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
    sender = os.getenv('SMTP_FROM') or user

    if not (host and user and pwd):
        logger.error('[smtp] missing SMTP_HOST / SMTP_USER / SMTP_PASSWORD')
        return False

    msg = EmailMessage()
    msg['From']    = f'Litrix <{sender}>'
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

