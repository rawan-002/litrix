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
    universal_search,
)
from . import campaign_views, my_reports_views

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
    path('search/',             universal_search,   name='universal-search'),

    # ---- Reporting Campaigns (admin) ----
    path('campaigns/',
         campaign_views.campaign_list_create,
         name='campaign-list-create'),
    path('campaigns/<int:campaign_id>/',
         campaign_views.campaign_detail,
         name='campaign-detail'),
    path('campaigns/<int:campaign_id>/open/',
         campaign_views.campaign_open,
         name='campaign-open'),
    path('campaigns/<int:campaign_id>/close/',
         campaign_views.campaign_close,
         name='campaign-close'),
    path('campaigns/<int:campaign_id>/submissions/',
         campaign_views.campaign_submissions,
         name='campaign-submissions'),
    path('campaigns/<int:campaign_id>/export/',
         campaign_views.campaign_export,
         name='campaign-export'),

    # ---- My Reports (researcher) ----
    path('my-reports/',
         my_reports_views.my_submissions,
         name='my-submissions'),
    path('my-reports/<int:submission_id>/',
         my_reports_views.submission_detail,
         name='submission-detail'),
    path('my-reports/<int:submission_id>/decisions/',
         my_reports_views.record_decision,
         name='submission-record-decision'),
    path('my-reports/<int:submission_id>/missing/',
         my_reports_views.add_missing,
         name='submission-add-missing'),
    path('my-reports/<int:submission_id>/decisions/<int:decision_id>/',
         my_reports_views.delete_decision,
         name='submission-delete-decision'),
    path('my-reports/<int:submission_id>/submit/',
         my_reports_views.submit_submission,
         name='submission-submit'),
]
