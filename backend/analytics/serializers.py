"""
DRF Serializers — convert ORM rows to JSON for the Angular frontend.

Each serializer here is intentionally minimal: it just lists fields. We
don't add custom logic because the views (the SQL ones) already did the
heavy lifting of aggregation and computation.
"""
from rest_framework import serializers
from .models import (
    ResearcherStats, DepartmentStats, TopPaper, PublicationTrend,
    ResearchPaper, Department,
    ReportCampaign, ReportSubmission, ReportPaperDecision,
    ScheduledNotification,
)


class ResearcherStatsSerializer(serializers.ModelSerializer):
    """The cornerstone payload for the Researcher dashboard."""
    class Meta:
        model = ResearcherStats
        fields = [
            'user_id', 'full_name_ar', 'full_name_en',
            'scholar_id', 'orcid_id', 'openalex_author_id',
            'academic_rank', 'department_id', 'department_name',
            'total_papers', 'papers_last_5_years',
            'total_citations', 'avg_citations_per_paper',
            'h_index', 'first_pub_year', 'last_pub_year',
            'q1_papers', 'cross_validated_papers',
            'scopus_papers', 'isi_papers', 'manual_papers',
            'last_synced_at',
        ]


class DepartmentStatsSerializer(serializers.ModelSerializer):
    class Meta:
        model = DepartmentStats
        fields = [
            'department_id', 'department_name', 'college_id',
            'total_researchers', 'active_researchers',
            'total_papers', 'total_citations', 'total_q1_papers',
            'total_q2_papers', 'total_q3_papers', 'total_q4_papers',
            'total_scopus_papers', 'total_isi_papers',
            'journal_papers', 'conference_papers',
            'avg_h_index', 'max_h_index',
        ]


class TopPaperSerializer(serializers.ModelSerializer):
    class Meta:
        model = TopPaper
        fields = [
            'paper_id', 'title', 'pub_year', 'doi', 'source', 'indexing',
            'citations', 'journal_name', 'quartile', 'impact_factor',
            'primary_author_ar', 'scraped_at',
        ]


class PublicationTrendSerializer(serializers.ModelSerializer):
    class Meta:
        model = PublicationTrend
        fields = [
            'department_id', 'department_name', 'year',
            'papers', 'citations', 'q1_papers',
        ]


class ResearchPaperSerializer(serializers.ModelSerializer):
    """Used when listing a researcher's individual papers."""
    class Meta:
        model = ResearchPaper
        fields = [
            'paper_id', 'title', 'title_en', 'abstract',
            'language', 'doi', 'pub_year', 'volume', 'issue', 'pages',
            'source', 'is_verified', 'scraped_at',
        ]


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['department_id', 'department_name', 'college_id']


# ============================================================================
# Sprint 7: Reporting Campaigns serializers
# ============================================================================
# Two-tier strategy:
#   • *Serializer        → full payload for detail endpoints
#   • *ListSerializer    → trimmed payload for list endpoints (avoid
#                          shipping huge JSON when the UI only renders cards)
#
# Decisions/Submissions intentionally keep their write/read on the same
# class — the volume is tiny and the savings of separate classes would
# not pay off.
# ============================================================================
class ReportCampaignSerializer(serializers.ModelSerializer):
    """
    Full campaign payload. Counts (`submissions_total` /
    `submissions_submitted`) are computed by the view and injected as
    extra context — we expose them here as read-only Integer fields so
    the schema is explicit at the API boundary.
    """
    submissions_total     = serializers.IntegerField(read_only=True, required=False)
    submissions_submitted = serializers.IntegerField(read_only=True, required=False)
    submissions_late      = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = ReportCampaign
        fields = [
            'campaign_id', 'tenant_id',
            'title', 'description',
            'target_years',
            'opens_at', 'closes_at',
            'status', 'scope_type', 'scope_filter',
            'created_by_user_id', 'created_at',
            'closed_at', 'archived_at',
            'submissions_total', 'submissions_submitted', 'submissions_late',
        ]
        read_only_fields = [
            'campaign_id', 'created_by_user_id', 'created_at',
            'closed_at', 'archived_at',
        ]

    def validate_target_years(self, value):
        """
        Guard against accidental bad shapes. The DB CHECK constraint
        catches an empty array too, but failing here gives the frontend
        a friendlier 400 instead of a Postgres-level 500.
        """
        if not isinstance(value, list) or len(value) == 0:
            raise serializers.ValidationError(
                'target_years must be a non-empty list of integers'
            )
        if not all(isinstance(y, int) for y in value):
            raise serializers.ValidationError(
                'target_years must contain integers only'
            )
        return value

    def validate(self, data):
        """Cross-field check: closes_at strictly after opens_at."""
        opens_at  = data.get('opens_at')  or getattr(self.instance, 'opens_at',  None)
        closes_at = data.get('closes_at') or getattr(self.instance, 'closes_at', None)
        if opens_at and closes_at and closes_at <= opens_at:
            raise serializers.ValidationError(
                {'closes_at': 'closes_at must be after opens_at'}
            )
        return data


class ReportPaperDecisionSerializer(serializers.ModelSerializer):
    """
    A single paper-level decision. Two shapes ride on the same model:

      1. existing paper:   { paper_id, decision: 'confirmed' | 'not_mine', note? }
      2. missing entry:    { decision: 'missing', missing_title, missing_year, missing_doi? }

    The DB CHECK constraint enforces the shape; we ALSO validate here
    so the API returns a friendly 400 instead of a 500.
    """
    class Meta:
        model = ReportPaperDecision
        fields = [
            'decision_id', 'submission_id', 'paper_id',
            'decision', 'note', 'decided_at',
            'missing_title', 'missing_doi', 'missing_year',
            'missing_resolved_at', 'missing_resolved_to_paper_id',
        ]
        read_only_fields = [
            'decision_id', 'decided_at',
            'missing_resolved_at', 'missing_resolved_to_paper_id',
        ]

    def validate(self, data):
        decision = data.get('decision') or getattr(self.instance, 'decision', None)
        if decision in ('confirmed', 'not_mine'):
            if not data.get('paper_id') and not getattr(self.instance, 'paper_id', None):
                raise serializers.ValidationError(
                    {'paper_id': f'paper_id is required for decision={decision}'}
                )
        elif decision == 'missing':
            if not data.get('missing_title'):
                raise serializers.ValidationError(
                    {'missing_title': 'missing_title is required when decision=missing'}
                )
            if not data.get('missing_year'):
                raise serializers.ValidationError(
                    {'missing_year': 'missing_year is required when decision=missing'}
                )
        return data


class ReportSubmissionSerializer(serializers.ModelSerializer):
    """
    A researcher's submission for one campaign. The full payload
    includes the auto-populated paper list + any existing decisions —
    but that join lives in the view (raw SQL is cheaper than the ORM
    here). This serializer is for the basic submission row only.
    """
    # Light denormalized view of the campaign so the frontend doesn't
    # need a second round-trip just to show "Annual Report 2025–2026".
    campaign_title    = serializers.CharField(
        source='campaign.title', read_only=True, required=False, default=None,
    )
    campaign_closes_at = serializers.DateTimeField(
        source='campaign.closes_at', read_only=True, required=False, default=None,
    )

    class Meta:
        model = ReportSubmission
        fields = [
            'submission_id', 'campaign_id', 'user_id',
            'status', 'started_at', 'submitted_at',
            'reopened_at', 'reopened_by_user_id',
            'is_late', 'admin_reviewed_at',
            'campaign_title', 'campaign_closes_at',
        ]
        read_only_fields = [
            'submission_id', 'campaign_id', 'user_id',
            'started_at', 'submitted_at',
            'reopened_at', 'reopened_by_user_id',
            'is_late', 'admin_reviewed_at',
        ]


class ScheduledNotificationSerializer(serializers.ModelSerializer):
    """
    Admin notification composer payload.

    `target_audience` is a free-form JSON spec the worker translates
    into a SELECT — examples in the model's docstring. Validation here
    is intentionally light because the worker tolerates missing/empty
    audiences (no recipients = no-op send).
    """
    class Meta:
        model = ScheduledNotification
        fields = [
            'schedule_id', 'tenant_id',
            'title', 'body',
            'notification_type', 'target_audience',
            'send_at', 'status',
            'sent_at', 'recipient_count', 'error_message',
            'related_campaign_id',
            'created_by_user_id', 'created_at',
        ]
        read_only_fields = [
            'schedule_id', 'status', 'sent_at',
            'recipient_count', 'error_message',
            'created_by_user_id', 'created_at',
        ]

    def validate_target_audience(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'target_audience must be a JSON object'
            )
        return value
