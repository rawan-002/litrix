"""
Analytics Models — read-only mappings to existing DB tables and views.

Architecture decision: every model here uses `managed = False`. This means:
    • Django will NEVER alter these tables/views (the scraper + migrations
      are the source of truth for schema changes).
    • `migrate` won't try to create them.
    • The ORM treats them as if they always exist.

The models map to:
    Domain tables (already populated by the scraper):
        Users, Researcher, Department, College, ResearchPaper, Authors,
        Journals, JournalRankings, Works_In, ExternalAuthors

    Analytics views (created by migration 005):
        v_researcher_stats, v_department_stats, v_top_papers,
        v_publication_trends, v_paper_citations, v_researcher_h_index
"""
from django.db import models


class User(models.Model):
    """Mirror of the 'Users' table (note the plural)."""
    user_id     = models.AutoField(primary_key=True, db_column='UserID')
    first_name  = models.CharField(max_length=100, db_column='FirstName')
    middle_name = models.CharField(max_length=100, db_column='MiddleName',
                                    null=True, blank=True)
    last_name   = models.CharField(max_length=100, db_column='LastName')
    full_name_ar = models.CharField(max_length=255, db_column='FullName_Ar',
                                    null=True, blank=True)
    email       = models.CharField(max_length=255, db_column='Email')
    user_type   = models.CharField(max_length=50,  db_column='UserType')
    account_status = models.CharField(max_length=50, db_column='AccountStatus')
    scholar_id  = models.CharField(max_length=50, db_column='Scholar_ID',
                                   null=True, blank=True)
    litrix_id   = models.CharField(max_length=50, db_column='Litrix_ID',
                                   null=True, blank=True)
    created_at  = models.DateTimeField(db_column='CreatedAt')

    class Meta:
        managed = False
        db_table = 'Users'

    def __str__(self):
        return self.full_name_ar or f"{self.first_name} {self.last_name}"


class Department(models.Model):
    department_id   = models.AutoField(primary_key=True, db_column='DepartmentID')
    department_name = models.CharField(max_length=255, db_column='DepartmentName')
    college_id      = models.IntegerField(db_column='CollegeID', null=True, blank=True)
    head_id         = models.IntegerField(db_column='HeadID', null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'Department'

    def __str__(self):
        return self.department_name


class ResearchPaper(models.Model):
    paper_id         = models.AutoField(primary_key=True, db_column='PaperID')
    journal_id       = models.IntegerField(db_column='JournalID', null=True, blank=True)
    title            = models.TextField(db_column='Title')
    title_en         = models.TextField(db_column='Title_En', null=True, blank=True)
    abstract         = models.TextField(db_column='Abstract', null=True, blank=True)
    language         = models.CharField(max_length=10, db_column='Language', null=True)
    doi              = models.CharField(max_length=100, db_column='DOI', null=True)
    pub_year         = models.IntegerField(db_column='PubYear', null=True, blank=True)
    volume           = models.CharField(max_length=50, db_column='Volume', null=True)
    issue            = models.CharField(max_length=50, db_column='Issue', null=True)
    pages            = models.CharField(max_length=100, db_column='Pages', null=True)
    is_verified      = models.BooleanField(db_column='IsVerified', default=False)
    scraped_at       = models.DateTimeField(db_column='ScrapedAt')
    source           = models.CharField(max_length=100, db_column='Source')
    normalized_title = models.TextField(db_column='NormalizedTitle', null=True)

    class Meta:
        managed = False
        db_table = 'ResearchPaper'
        ordering = ['-pub_year', 'paper_id']


class ResearcherStats(models.Model):
    """Maps to v_researcher_stats. The cornerstone view."""
    user_id              = models.IntegerField(primary_key=True)
    full_name_ar         = models.CharField(max_length=255, null=True)
    full_name_en         = models.CharField(max_length=255, null=True)
    scholar_id           = models.CharField(max_length=50, null=True)
    orcid_id             = models.CharField(max_length=50, null=True)
    openalex_author_id   = models.CharField(max_length=50, null=True)
    academic_rank        = models.CharField(max_length=100, null=True)
    last_synced_at       = models.DateTimeField(null=True)
    department_id        = models.IntegerField(null=True)
    department_name      = models.CharField(max_length=255, null=True)
    total_papers         = models.IntegerField()
    papers_last_5_years  = models.IntegerField()
    total_citations      = models.IntegerField()
    avg_citations_per_paper = models.DecimalField(max_digits=10, decimal_places=2)
    h_index              = models.IntegerField()
    first_pub_year       = models.IntegerField(null=True)
    last_pub_year        = models.IntegerField(null=True)
    q1_papers            = models.IntegerField()
    cross_validated_papers = models.IntegerField()
    scopus_papers        = models.IntegerField(default=0)
    isi_papers           = models.IntegerField(default=0)
    manual_papers        = models.IntegerField(default=0)

    class Meta:
        managed = False
        db_table = 'v_researcher_stats'

    def __str__(self):
        return self.full_name_ar or self.full_name_en or f"Researcher {self.user_id}"


class DepartmentStats(models.Model):
    """Maps to v_department_stats."""
    department_id        = models.IntegerField(primary_key=True)
    department_name      = models.CharField(max_length=255)
    college_id           = models.IntegerField(null=True)
    total_researchers    = models.IntegerField()
    active_researchers   = models.IntegerField()
    total_papers         = models.IntegerField()
    total_citations      = models.IntegerField()
    total_q1_papers      = models.IntegerField()
    total_scopus_papers  = models.IntegerField(default=0)
    total_isi_papers     = models.IntegerField(default=0)
    avg_h_index          = models.DecimalField(max_digits=10, decimal_places=2)
    max_h_index          = models.IntegerField()

    class Meta:
        managed = False
        db_table = 'v_department_stats'

    def __str__(self):
        return self.department_name


class TopPaper(models.Model):
    """Maps to v_top_papers — the leaderboard view."""
    paper_id          = models.IntegerField(primary_key=True)
    title             = models.TextField()
    pub_year          = models.IntegerField(null=True)
    doi               = models.CharField(max_length=100, null=True)
    source            = models.CharField(max_length=100, null=True)
    indexing          = models.CharField(max_length=50, null=True)
    citations         = models.IntegerField()
    journal_name      = models.CharField(max_length=500, null=True)
    quartile          = models.CharField(max_length=5, null=True)
    impact_factor     = models.FloatField(null=True)
    primary_author_ar = models.CharField(max_length=255, null=True)
    scraped_at        = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = 'v_top_papers'
        ordering = ['-citations']


class PublicationTrend(models.Model):
    """Maps to v_publication_trends. Composite key (department_id, year)."""
    department_id   = models.IntegerField(primary_key=True)
    department_name = models.CharField(max_length=255)
    year            = models.IntegerField()
    papers          = models.IntegerField()
    citations       = models.IntegerField()
    q1_papers       = models.IntegerField()

    class Meta:
        managed = False
        db_table = 'v_publication_trends'
        ordering = ['department_name', 'year']
