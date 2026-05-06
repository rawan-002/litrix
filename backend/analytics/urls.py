"""
Analytics URL routing.

Pattern: DRF's DefaultRouter auto-generates list+detail URLs for each
ViewSet. The result:

    /api/researchers/
    /api/researchers/{user_id}/
    /api/researchers/{user_id}/papers/   ← from @action

    /api/departments/
    /api/departments/{id}/
    /api/departments/{id}/researchers/   ← from @action

    /api/papers/top/
    /api/papers/top/{paper_id}/

    /api/trends/

    /api/stats/overview/                 ← function view

The auto-generated browsable API at /api/ shows the full route map.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ResearcherViewSet, DepartmentViewSet,
    TopPaperViewSet, PublicationTrendViewSet,
    overview, yearly_breakdown, export_excel, paper_detail,
)

router = DefaultRouter()
router.register(r'researchers',  ResearcherViewSet,       basename='researcher')
router.register(r'departments',  DepartmentViewSet,       basename='department')
router.register(r'papers/top',   TopPaperViewSet,         basename='top-paper')
router.register(r'trends',       PublicationTrendViewSet, basename='trend')

urlpatterns = [
    path('', include(router.urls)),
    path('stats/overview/',     overview,           name='overview'),
    path('yearly-breakdown/',   yearly_breakdown,   name='yearly-breakdown'),
    path('export/excel/',       export_excel,       name='export-excel'),
    path('papers/<int:paper_id>/detail/', paper_detail, name='paper-detail'),
]
