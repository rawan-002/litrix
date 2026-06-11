"""
DRF serializers — ORM rows to JSON for the frontend.

Mostly just field lists: the raw-SQL views already do the aggregation, so
there's little for the serializers to compute.
"""
from rest_framework import serializers
from .models import (
    ResearcherStats, DepartmentStats, TopPaper, PublicationTrend,
    ResearchPaper, Department,
    ReportCampaign, ReportSubmission, ReportPaperDecision,
    ScheduledNotification,
)


class ResearcherStatsSerializer(serializers.ModelSerializer):
    """Main payload for the researcher dashboard."""
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


# Reporting Campaigns serializers. Decisions/Submissions keep read and write
# on one class — the volume is tiny, so splitting wouldn't pay off.
class ReportCampaignSerializer(serializers.ModelSerializer):
    """
    Full campaign payload. The submission counts are computed in the view;
    we declare them here as read-only Integer fields so the schema stays
    explicit at the API boundary.
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
        The DB CHECK catches an empty array too, but validating here turns
        it into a friendly 400 instead of a Postgres 500.
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
        """closes_at must be strictly after opens_at."""
        opens_at  = data.get('opens_at')  or getattr(self.instance, 'opens_at',  None)
        closes_at = data.get('closes_at') or getattr(self.instance, 'closes_at', None)
        if opens_at and closes_at and closes_at <= opens_at:
            raise serializers.ValidationError(
                {'closes_at': 'closes_at must be after opens_at'}
            )
        return data


class ReportPaperDecisionSerializer(serializers.ModelSerializer):
    """
    A single paper-level decision. Two shapes on one model:
      existing paper: { paper_id, decision: 'confirmed' | 'not_mine', note? }
      missing entry:  { decision: 'missing', missing_title, missing_year, missing_doi? }

    The DB CHECK enforces the shape; we validate here too for a friendly 400.
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
    A researcher's submission for one campaign — the basic row only. The
    auto-populated paper list + decisions are joined in the view (raw SQL is
    cheaper than the ORM here).
    """
    # A little campaign denorm so the frontend can show the title without a
    # second round-trip.
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
    Admin notification-composer payload.

    target_audience is a free-form JSON spec the worker turns into a SELECT
    (examples in the model docstring). Validation stays light because the
    worker tolerates an empty audience — no recipients just means no send.
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
