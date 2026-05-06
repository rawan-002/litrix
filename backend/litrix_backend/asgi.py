"""
ASGI config for litrix_backend project.
Used by ASGI servers like uvicorn (for async or websockets later).
"""
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')

application = get_asgi_application()
