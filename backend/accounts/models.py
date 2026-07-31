from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models, connection, transaction


def generate_litrix_id() -> str:
    """Next sequential Litrix_ID (Lit-NNNNNN).

    The DB trigger fn_assign_litrix_id is canonical and fills in any NULL,
    but app code (admin scripts, tests) sometimes needs the value up front.
    """
    with connection.cursor() as cur:
        cur.execute('''
            SELECT COALESCE(
                MAX(CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER)),
                0
            ) + 1
            FROM "Users"
            WHERE "Litrix_ID" ~ '^Lit-[0-9]+$'
        ''')
        next_seq = cur.fetchone()[0]
    return f'Lit-{next_seq:06d}'


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError('Email required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        # Assign litrix_id in the same transaction so it's set on the
        # returned instance. The DB trigger still covers paths that skip
        # this manager (raw SQL, bulk_create).
        with transaction.atomic(using=self._db):
            if not user.litrix_id:
                user.litrix_id = generate_litrix_id()
            user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra):
        extra.setdefault('user_type', 'Admin')
        extra.setdefault('account_status', 'Active')
        extra.setdefault('is_active', True)
        extra.setdefault('email_verified', True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser):
    user_id        = models.AutoField(primary_key=True, db_column='UserID')
    email          = models.EmailField(unique=True, db_column='Email')
    password       = models.CharField(max_length=255, db_column='PasswordHash', null=True, blank=True)

    first_name     = models.CharField(max_length=100, db_column='FirstName',  null=True, blank=True)
    middle_name    = models.CharField(max_length=100, db_column='MiddleName', null=True, blank=True)
    last_name      = models.CharField(max_length=100, db_column='LastName',   null=True, blank=True)
    full_name_ar   = models.CharField(max_length=255, db_column='FullName_Ar', null=True, blank=True)
    scholar_display_name = models.CharField(max_length=255, db_column='ScholarDisplayName',
                                             null=True, blank=True)

    user_type      = models.CharField(max_length=50, db_column='UserType')
    account_status = models.CharField(max_length=50, db_column='AccountStatus')

    tenant_id      = models.IntegerField(default=1, db_column='TenantID')
    role_id        = models.IntegerField(null=True, blank=True, db_column='RoleID')
    email_verified = models.BooleanField(default=False, db_column='EmailVerified')
    is_active      = models.BooleanField(default=True, db_column='IsActive')
    last_login     = models.DateTimeField(null=True, blank=True, db_column='LastLoginAt')

    scholar_id     = models.CharField(max_length=64, null=True, blank=True, db_column='Scholar_ID')
    orcid_id       = models.CharField(max_length=64, null=True, blank=True, db_column='Orcid_ID')
    scopus_id      = models.CharField(max_length=64, null=True, blank=True, db_column='Scopus_ID')
    litrix_id      = models.CharField(max_length=50, null=True, blank=True, db_column='Litrix_ID')
    photo_url      = models.TextField(null=True, blank=True, db_column='PhotoURL')

    created_at     = models.DateTimeField(db_column='CreatedAt', auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        managed = False
        db_table = 'Users'

    def __str__(self):
        return self.get_full_name() or self.email

    @property
    def is_staff(self):
        return self.user_type in ('Admin', 'Dean', 'HoD')

    @property
    def is_superuser(self):
        return self.user_type == 'Admin'

    def get_full_name(self):
        # Primary display name is the EXACT name Google Scholar shows on the
        # researcher's profile (ScholarDisplayName, captured verbatim by
        # citations/backfill_scholar_names.py). FullName_Ar is kept in the
        # table but never shown in the UI. first/last sometimes hold an
        # Arabic short form instead of a genuine English name, so guard for
        # that in the fallback used when there's no Scholar name yet.
        if self.scholar_display_name:
            return self.scholar_display_name
        en = f'{self.first_name or ""} {self.last_name or ""}'.strip()
        if en and en.isascii() and any(c.isalpha() for c in en):
            return en
        return self.email

    def get_short_name(self):
        return self.first_name or self.email

    def get_role(self):
        if not self.role_id:
            return None
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('SELECT "Name" FROM "Role" WHERE "RoleID" = %s', [self.role_id])
            row = cur.fetchone()
            return row[0] if row else None

    def get_permissions(self):
        if not self.role_id:
            return set()
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('''
                SELECT p."Code" FROM "Permission" p
                JOIN "RolePermission" rp ON rp."PermissionID" = p."PermissionID"
                WHERE rp."RoleID" = %s
            ''', [self.role_id])
            return {row[0] for row in cur.fetchall()}

    def has_litrix_perm(self, code):
        return code in self.get_permissions()

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_perms(self, perm_list, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser

    @property
    def is_anonymous(self):
        return False

    @property
    def is_authenticated(self):
        return True
