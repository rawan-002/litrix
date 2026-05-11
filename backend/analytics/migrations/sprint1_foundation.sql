BEGIN;

CREATE TABLE IF NOT EXISTS "Tenant" (
    "TenantID"        SERIAL PRIMARY KEY,
    "Name"            VARCHAR(255) NOT NULL,
    "Slug"            VARCHAR(64)  NOT NULL UNIQUE,
    "Subdomain"       VARCHAR(64)  UNIQUE,
    "CustomDomain"    VARCHAR(255) UNIQUE,
    "LogoUrl"         TEXT,
    "PrimaryColor"    VARCHAR(16) DEFAULT '#1D1D1F',
    "SecondaryColor" VARCHAR(16) DEFAULT '#86868B',
    "Status"          VARCHAR(32)  DEFAULT 'active',
    "SubscriptionTier" VARCHAR(32) DEFAULT 'free',
    "Settings"        JSONB        DEFAULT '{}'::jsonb,
    "Timezone"        VARCHAR(64)  DEFAULT 'Asia/Riyadh',
    "CreatedAt"       TIMESTAMPTZ  DEFAULT NOW(),
    "UpdatedAt"       TIMESTAMPTZ  DEFAULT NOW()
);

INSERT INTO "Tenant" ("TenantID", "Name", "Slug", "Subdomain", "Status", "SubscriptionTier")
VALUES (1, 'Al-Baha University - College of Computing & IT', 'albaha-ccit', 'albaha', 'active', 'enterprise')
ON CONFLICT ("TenantID") DO NOTHING;

SELECT setval('"Tenant_TenantID_seq"', GREATEST((SELECT MAX("TenantID") FROM "Tenant"), 1));


CREATE TABLE IF NOT EXISTS "Role" (
    "RoleID"      SERIAL PRIMARY KEY,
    "TenantID"    INTEGER REFERENCES "Tenant"("TenantID") ON DELETE CASCADE,
    "Name"        VARCHAR(64) NOT NULL,
    "Description" TEXT,
    "IsSystem"    BOOLEAN DEFAULT FALSE,
    "CreatedAt"   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE ("TenantID", "Name")
);

CREATE TABLE IF NOT EXISTS "Permission" (
    "PermissionID" SERIAL PRIMARY KEY,
    "Code"         VARCHAR(128) NOT NULL UNIQUE,
    "Description"  TEXT,
    "Category"     VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS "RolePermission" (
    "RoleID"       INTEGER REFERENCES "Role"("RoleID") ON DELETE CASCADE,
    "PermissionID" INTEGER REFERENCES "Permission"("PermissionID") ON DELETE CASCADE,
    PRIMARY KEY ("RoleID", "PermissionID")
);


INSERT INTO "Permission" ("Code", "Description", "Category") VALUES
    ('view_dashboard',          'View main dashboard',                    'dashboard'),
    ('view_all_researchers',    'View all researchers across departments','researchers'),
    ('view_dept_researchers',   'View researchers in own department',     'researchers'),
    ('edit_own_profile',        'Edit own profile and links',             'profile'),
    ('edit_any_profile',        'Edit any researcher profile',            'profile'),
    ('approve_registrations',   'Approve/reject registration requests',   'admin'),
    ('manage_users',            'Create, edit, delete users',             'admin'),
    ('manage_roles',            'Create roles, assign permissions',       'admin'),
    ('manage_departments',      'Create, edit departments',               'admin'),
    ('trigger_sync',            'Trigger Scholar/OpenAlex sync',          'admin'),
    ('view_audit_log',          'View audit log',                         'admin'),
    ('export_excel',            'Export Excel reports',                   'export'),
    ('export_dept_excel',       'Export Excel for own department',        'export'),
    ('view_notifications',      'View notifications',                     'notifications'),
    ('manage_tenant_settings',  'Edit tenant branding and settings',      'tenant')
ON CONFLICT ("Code") DO NOTHING;


INSERT INTO "Role" ("TenantID", "Name", "Description", "IsSystem") VALUES
    (1, 'Admin',      'Full platform access for this tenant',  TRUE),
    (1, 'Dean',       'College-wide read access + exports',     TRUE),
    (1, 'HoD',        'Head of department — manages own dept', TRUE),
    (1, 'Researcher', 'Standard researcher self-service',       TRUE)
ON CONFLICT DO NOTHING;


WITH r AS (SELECT "RoleID" FROM "Role" WHERE "Name" = 'Admin' AND "TenantID" = 1)
INSERT INTO "RolePermission" ("RoleID", "PermissionID")
SELECT r."RoleID", p."PermissionID" FROM r, "Permission" p
ON CONFLICT DO NOTHING;

WITH r AS (SELECT "RoleID" FROM "Role" WHERE "Name" = 'Dean' AND "TenantID" = 1)
INSERT INTO "RolePermission" ("RoleID", "PermissionID")
SELECT r."RoleID", p."PermissionID" FROM r, "Permission" p
WHERE p."Code" IN (
    'view_dashboard', 'view_all_researchers', 'edit_own_profile',
    'export_excel', 'view_notifications'
)
ON CONFLICT DO NOTHING;

WITH r AS (SELECT "RoleID" FROM "Role" WHERE "Name" = 'HoD' AND "TenantID" = 1)
INSERT INTO "RolePermission" ("RoleID", "PermissionID")
SELECT r."RoleID", p."PermissionID" FROM r, "Permission" p
WHERE p."Code" IN (
    'view_dashboard', 'view_dept_researchers', 'edit_own_profile',
    'approve_registrations', 'export_dept_excel', 'view_notifications'
)
ON CONFLICT DO NOTHING;

WITH r AS (SELECT "RoleID" FROM "Role" WHERE "Name" = 'Researcher' AND "TenantID" = 1)
INSERT INTO "RolePermission" ("RoleID", "PermissionID")
SELECT r."RoleID", p."PermissionID" FROM r, "Permission" p
WHERE p."Code" IN (
    'view_dashboard', 'edit_own_profile', 'view_notifications'
)
ON CONFLICT DO NOTHING;


ALTER TABLE "Users"
    ADD COLUMN IF NOT EXISTS "TenantID"      INTEGER DEFAULT 1 REFERENCES "Tenant"("TenantID"),
    ADD COLUMN IF NOT EXISTS "RoleID"        INTEGER REFERENCES "Role"("RoleID"),
    ADD COLUMN IF NOT EXISTS "EmailVerified" BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS "PasswordHash"  VARCHAR(255),
    ADD COLUMN IF NOT EXISTS "IsActive"      BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS "LastLoginAt"   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS "Orcid_ID"      VARCHAR(64),
    ADD COLUMN IF NOT EXISTS "Scopus_ID"     VARCHAR(64);

UPDATE "Users" SET "TenantID" = 1 WHERE "TenantID" IS NULL;

UPDATE "Users" u
SET "RoleID" = (SELECT "RoleID" FROM "Role" WHERE "Name" = 'Researcher' AND "TenantID" = 1)
WHERE u."UserType" = 'Researcher' AND u."RoleID" IS NULL;

UPDATE "Users" u
SET "RoleID" = (SELECT "RoleID" FROM "Role" WHERE "Name" = 'Admin' AND "TenantID" = 1)
WHERE u."UserType" = 'Admin' AND u."RoleID" IS NULL;


ALTER TABLE "Department"
    ADD COLUMN IF NOT EXISTS "TenantID" INTEGER DEFAULT 1 REFERENCES "Tenant"("TenantID");
UPDATE "Department" SET "TenantID" = 1 WHERE "TenantID" IS NULL;

ALTER TABLE "ResearchPaper"
    ADD COLUMN IF NOT EXISTS "TenantID" INTEGER DEFAULT 1 REFERENCES "Tenant"("TenantID");
UPDATE "ResearchPaper" SET "TenantID" = 1 WHERE "TenantID" IS NULL;


CREATE TABLE IF NOT EXISTS "EmailVerification" (
    "TokenID"     SERIAL PRIMARY KEY,
    "Email"       VARCHAR(255) NOT NULL,
    "Token"       VARCHAR(128) NOT NULL UNIQUE,
    "TenantID"    INTEGER REFERENCES "Tenant"("TenantID"),
    "Purpose"     VARCHAR(32) NOT NULL DEFAULT 'registration',
    "ExpiresAt"   TIMESTAMPTZ NOT NULL,
    "UsedAt"      TIMESTAMPTZ,
    "CreatedAt"   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_emailverif_token ON "EmailVerification"("Token");
CREATE INDEX IF NOT EXISTS idx_emailverif_email ON "EmailVerification"("Email");


CREATE TABLE IF NOT EXISTS "RegistrationRequest" (
    "RequestID"        SERIAL PRIMARY KEY,
    "TenantID"         INTEGER REFERENCES "Tenant"("TenantID") DEFAULT 1,
    "Email"            VARCHAR(255) NOT NULL,
    "FullName_Ar"      VARCHAR(255),
    "FullName_En"      VARCHAR(255),
    "Scholar_ID"       VARCHAR(64),
    "Orcid_ID"         VARCHAR(64),
    "Scopus_ID"        VARCHAR(64),
    "DepartmentID"     INTEGER REFERENCES "Department"("DepartmentID"),
    "AcademicRank"     VARCHAR(64),
    "Status"           VARCHAR(32) DEFAULT 'pending',
    "ReviewedByUserID" INTEGER REFERENCES "Users"("UserID"),
    "ReviewedAt"       TIMESTAMPTZ,
    "RejectionReason"  TEXT,
    "PasswordHash"     VARCHAR(255),
    "CreatedAt"        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_regreq_status ON "RegistrationRequest"("Status");
CREATE INDEX IF NOT EXISTS idx_regreq_tenant ON "RegistrationRequest"("TenantID");


CREATE TABLE IF NOT EXISTS "Notification" (
    "NotificationID" SERIAL PRIMARY KEY,
    "TenantID"       INTEGER REFERENCES "Tenant"("TenantID"),
    "UserID"         INTEGER REFERENCES "Users"("UserID") ON DELETE CASCADE,
    "Type"           VARCHAR(64) NOT NULL,
    "Title"          VARCHAR(255) NOT NULL,
    "Message"        TEXT,
    "Metadata"       JSONB DEFAULT '{}'::jsonb,
    "IsRead"         BOOLEAN DEFAULT FALSE,
    "CreatedAt"      TIMESTAMPTZ DEFAULT NOW(),
    "ReadAt"         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notif_user_unread ON "Notification"("UserID", "IsRead");
CREATE INDEX IF NOT EXISTS idx_notif_tenant ON "Notification"("TenantID");


CREATE TABLE IF NOT EXISTS "AuditLog" (
    "LogID"        SERIAL PRIMARY KEY,
    "TenantID"     INTEGER REFERENCES "Tenant"("TenantID"),
    "UserID"       INTEGER REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "Action"       VARCHAR(128) NOT NULL,
    "TargetType"   VARCHAR(64),
    "TargetID"     INTEGER,
    "Metadata"     JSONB DEFAULT '{}'::jsonb,
    "IpAddress"    VARCHAR(64),
    "UserAgent"    TEXT,
    "CreatedAt"    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_date ON "AuditLog"("TenantID", "CreatedAt" DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON "AuditLog"("UserID");
CREATE INDEX IF NOT EXISTS idx_audit_action ON "AuditLog"("Action");


CREATE TABLE IF NOT EXISTS "RefreshToken" (
    "TokenID"    SERIAL PRIMARY KEY,
    "UserID"     INTEGER REFERENCES "Users"("UserID") ON DELETE CASCADE,
    "TokenHash"  VARCHAR(255) NOT NULL UNIQUE,
    "ExpiresAt"  TIMESTAMPTZ NOT NULL,
    "RevokedAt"  TIMESTAMPTZ,
    "IpAddress"  VARCHAR(64),
    "UserAgent"  TEXT,
    "CreatedAt"  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON "RefreshToken"("UserID");


CREATE INDEX IF NOT EXISTS idx_users_tenant ON "Users"("TenantID");
CREATE INDEX IF NOT EXISTS idx_users_email ON "Users"("Email");
CREATE INDEX IF NOT EXISTS idx_users_role ON "Users"("RoleID");
CREATE INDEX IF NOT EXISTS idx_dept_tenant ON "Department"("TenantID");
CREATE INDEX IF NOT EXISTS idx_paper_tenant ON "ResearchPaper"("TenantID");

COMMIT;
