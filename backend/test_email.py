"""
Quick test script to verify the email backend is wired up.

USAGE:
    python test_email.py your-email@example.com
"""
import os, sys
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from accounts.email_service import send_email


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_email.py recipient@example.com")
        sys.exit(1)

    to = sys.argv[1]
    backend = os.getenv("EMAIL_BACKEND", "console")

    print(f"Backend  : {backend}")
    print(f"From     : {os.getenv('SMTP_FROM') or os.getenv('SMTP_USER') or '(default)'}")
    print(f"To       : {to}")
    print(f"Sending  ...")

    ok = send_email(
        to=to,
        subject="Litrix - email service test",
        body=(
            "Hello,\n\n"
            "This is a test email from Litrix's transactional email service. "
            "If you're reading this, the SMTP backend is wired up correctly.\n\n"
            "You can now expect verification emails, password resets, and "
            "invitation emails to land in your inbox.\n\n"
            "— Litrix"
        ),
    )

    if ok:
        print("\n[ok] email sent. Check the recipient's inbox + spam.")
    else:
        print("\n[fail] email NOT sent. Check the error above in the logs.")


if __name__ == "__main__":
    main()
