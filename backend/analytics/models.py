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


# ============================================================================
# Sprint 7: Reporting Campaigns
# ============================================================================
# Tables created by analytics/migrations/sprint7_reports.sql. They live
# in the same DB as the domain tables but are application-owned (we
# wrote them, the scraper doesn't touch them). Still `managed = False`
# to keep the project-wide convention — schema lives in SQL files, not
# Django's autogenerated migrations.
# ============================================================================
class ReportCampaign(models.Model):
    """
    A verification window opened by an admin.

    State machine:
        draft → [open()] → active → [auto-close at closes_at] → closed → archived
                                 ↘ [reopen()] ↗

    Transitions are enforced in views (not at the DB layer) so we can
    emit audit log entries + side-effects (notification fan-out) atomically.
    """
    STATUS_CHOICES = [
        ('draft',    'Draft'),
        ('active',   'Active'),
        ('closed',   'Closed'),
        ('archived', 'Archived'),
    ]
    SCOPE_CHOICES = [
        ('all',        'All Researchers'),
        ('department', 'Department(s)'),
        ('custom',     'Custom User List'),
    ]

    campaign_id      = models.AutoField(primary_key=True, db_column='CampaignID')
    tenant_id        = models.IntegerField(db_column='TenantID', default=1)
    title            = models.CharField(max_length=200, db_column='Title')
    description      = models.TextField(db_column='Description', null=True, blank=True)

    # Postgres int[] — ArrayField from contrib.postgres. We hold off on
    # importing it explicitly because the field is JSON-serializable as a
    # plain list via raw SQL, which is how the views read/write it. The
    # ORM-level mapping below is JSONField so we keep dependency-free.
    target_years     = models.JSONField(db_column='TargetYears')

    opens_at         = models.DateTimeField(db_column='OpensAt')
    closes_at        = models.DateTimeField(db_column='ClosesAt')

    status           = models.CharField(
        max_length=20, db_column='Status',
        default='draft', choices=STATUS_CHOICES,
    )
    scope_type       = models.CharField(
        max_length=20, db_column='ScopeType',
        default='all', choices=SCOPE_CHOICES,
    )
    scope_filter     = models.JSONField(db_column='ScopeFilter', null=True, blank=True)

    created_by_user_id = models.IntegerField(
        db_column='CreatedByUserID', null=True, blank=True,
    )
    created_at       = models.DateTimeField(db_column='CreatedAt', auto_now_add=True)
    closed_at        = models.DateTimeField(db_column='ClosedAt', null=True, blank=True)
    archived_at      = models.DateTimeField(db_column='ArchivedAt', null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'ReportCampaign'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.status})'


class ReportSubmission(models.Model):
    """
    A researcher's submission inside a campaign. Created automatically
    when a campaign is opened — one row per researcher in the scope.

    The IsLate flag is set at submit time (not via a generated column)
    because admins may override after the deadline.
    """
    STATUS_CHOICES = [
        ('pending',     'Pending'),
        ('in_progress', 'In Progress'),
        ('submitted',   'Submitted'),
        ('reopened',    'Reopened'),
    ]

    submission_id    = models.AutoField(primary_key=True, db_column='SubmissionID')
    campaign_id      = models.IntegerField(db_column='CampaignID')
    user_id          = models.IntegerField(db_column='UserID')
    status           = models.CharField(
        max_length=20, db_column='Status',
        default='pending', choices=STATUS_CHOICES,
    )
    started_at       = models.DateTimeField(db_column='StartedAt', null=True, blank=True)
    submitted_at     = models.DateTimeField(db_column='SubmittedAt', null=True, blank=True)
    reopened_at      = models.DateTimeField(db_column='ReopenedAt', null=True, blank=True)
    reopened_by_user_id = models.IntegerField(
        db_column='ReopenedByUserID', null=True, blank=True,
    )
    is_late          = models.BooleanField(db_column='IsLate', default=False)
    admin_reviewed_at = models.DateTimeField(
        db_column='AdminReviewedAt', null=True, blank=True,
    )

    class Meta:
        managed = False
        db_table = 'ReportSubmission'
        # Postgres-level UNIQUE(CampaignID, UserID) already enforces this;
        # the meta tuple is here so Django form validation respects it.
        unique_together = [('campaign_id', 'user_id')]


class ReportPaperDecision(models.Model):
    """
    One researcher's verdict on one paper inside a submission.

    For Decision='missing', PaperID is NULL and the MissingTitle/Year
    fields carry the researcher-supplied metadata. The admin resolves
    these later by linking to an existing PaperID or inserting a new
    ResearchPaper row.
    """
    DECISION_CHOICES = [
        ('confirmed', 'Confirmed'),
        ('not_mine',  'Not Mine'),
        ('missing',   'Missing'),
    ]

    decision_id      = models.AutoField(primary_key=True, db_column='DecisionID')
    submission_id    = models.IntegerField(db_column='SubmissionID')
    paper_id         = models.IntegerField(db_column='PaperID', null=True, blank=True)
    decision         = models.CharField(
        max_length=20, db_column='Decision', choices=DECISION_CHOICES,
    )
    note             = models.TextField(db_column='Note', null=True, blank=True)
    decided_at       = models.DateTimeField(db_column='DecidedAt', auto_now=True)

    missing_title    = models.TextField(db_column='MissingTitle', null=True, blank=True)
    missing_doi      = models.TextField(db_column='MissingDOI', null=True, blank=True)
    missing_year     = models.IntegerField(db_column='MissingYear', null=True, blank=True)
    missing_resolved_at = models.DateTimeField(
        db_column='MissingResolvedAt', null=True, blank=True,
    )
    missing_resolved_to_paper_id = models.IntegerField(
        db_column='MissingResolvedToPaperID', null=True, blank=True,
    )

    class Meta:
        managed = False
        db_table = 'ReportPaperDecision'


class ScheduledNotification(models.Model):
    """
    Outbound notification queue — the admin composer writes here, a
    periodic worker (`python manage.py send_due_notifications`) reads
    pending rows and expands them into per-user `Notification` rows.
    """
    STATUS_CHOICES = [
        ('pending',    'Pending'),
        ('processing', 'Processing'),
        ('sent',       'Sent'),
        ('failed',     'Failed'),
        ('cancelled',  'Cancelled'),
    ]

    schedule_id      = models.AutoField(primary_key=True, db_column='ScheduleID')
    tenant_id        = models.IntegerField(db_column='TenantID', default=1)
    title            = models.CharField(max_length=200, db_column='Title')
    body             = models.TextField(db_column='Body')
    notification_type = models.CharField(
        max_length=50, db_column='NotificationType', default='custom',
    )
    target_audience  = models.JSONField(db_column='TargetAudience')
    send_at          = models.DateTimeField(db_column='SendAt')
    status           = models.CharField(
        max_length=20, db_column='Status',
        default='pending', choices=STATUS_CHOICES,
    )
    sent_at          = models.DateTimeField(db_column='SentAt', null=True, blank=True)
    recipient_count  = models.IntegerField(db_column='RecipientCount', null=True, blank=True)
    error_message    = models.TextField(db_column='ErrorMessage', null=True, blank=True)
    related_campaign_id = models.IntegerField(
        db_column='RelatedCampaignID', null=True, blank=True,
    )
    created_by_user_id = models.IntegerField(
        db_column='CreatedByUserID', null=True, blank=True,
    )
    created_at       = models.DateTimeField(db_column='CreatedAt', auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'ScheduledNotification'
        ordering = ['-created_at']
