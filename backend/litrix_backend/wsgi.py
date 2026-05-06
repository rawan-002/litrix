"""
WSGI config for litrix_backend project.
Used by production WSGI servers (gunicorn, uwsgi).
"""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')

application = get_wsgi_application()
