-- ============================================================================
-- Sprint 6 — Role-scoped invitations.
-- ============================================================================
-- Why?
--   Admin-driven onboarding for HoD / Dean: instead of a researcher
--   self-signing-up and waiting for promotion, the admin generates a
--   token-bearing link bound to (email, role, department). The invitee
--   uses the link to register, and the resulting User is provisioned
--   directly with the intended role — bypassing the regular approval
--   queue, since the invite IS the approval.
--
-- Idempotent — safe to re-run.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS "Invitation" (
    "InvitationID"          SERIAL PRIMARY KEY,
    "TenantID"              INTEGER REFERENCES "Tenant"("TenantID") DEFAULT 1,
    "Token"                 VARCHAR(80)  NOT NULL UNIQUE,
    "InvitedEmail"          VARCHAR(255) NOT NULL,
    "IntendedRoleID"        INTEGER REFERENCES "Role"("RoleID"),
    "IntendedUserType"      VARCHAR(50)  NOT NULL,
    "IntendedDepartmentID"  INTEGER REFERENCES "Department"("DepartmentID"),
    "InvitedByUserID"       INTEGER REFERENCES "Users"("UserID"),
    "ExpiresAt"             TIMESTAMPTZ NOT NULL,
    "UsedAt"                TIMESTAMPTZ,
    "UsedByUserID"          INTEGER REFERENCES "Users"("UserID"),
    "RevokedAt"             TIMESTAMPTZ,
    "CreatedAt"             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_invitation_token  ON "Invitation"("Token");
CREATE INDEX IF NOT EXISTS idx_invitation_email  ON "Invitation"(LOWER("InvitedEmail"));
CREATE INDEX IF NOT EXISTS idx_invitation_status ON "Invitation"("UsedAt", "RevokedAt", "ExpiresAt");

COMMIT;
