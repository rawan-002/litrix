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
            'total_scopus_papers', 'total_isi_papers',
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
