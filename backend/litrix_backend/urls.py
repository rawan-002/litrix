"""
Litrix Backend URL routing.

Top-level routes:
    /admin/       → Django admin (for emergency DB inspection)
    /api/         → DRF endpoints (analytics app)
    /api-auth/    → DRF browsable login/logout (dev only)
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('analytics.urls')),
    path('api-auth/', include('rest_framework.urls')),
]
