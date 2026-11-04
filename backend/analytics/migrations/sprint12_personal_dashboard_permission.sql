-- =============================================================================
-- Sprint 12 — "View personal dashboard" permission
-- =============================================================================
-- Until now DashboardRouterComponent (frontend) picked the home-page
-- dashboard purely by view_all_researchers/view_dept_researchers: anyone
-- with either saw ONLY the institution-wide Overview Dashboard, with no way
-- back to their own personal (researcher) dashboard - relevant for a user
-- who is genuinely both an admin AND a working researcher (promoted to the
-- Admin role but still wants their own Publications/Citations/Q1 view and
-- the personal Export).
--
-- This permission does not gate any API - a user can always read/export
-- their own data regardless (same as edit_own_profile). It only controls
-- whether the frontend offers a tab to switch to the personal dashboard
-- alongside the institution overview.
-- =============================================================================

INSERT INTO "Permission" ("Code", "Description", "Category")
VALUES
    ('view_personal_dashboard',
     'View own personal researcher dashboard alongside the institution overview',
     'dashboard')
ON CONFLICT ("Code") DO NOTHING;

-- Default: on for Admin only (the role this was built for). HoD/Dean admins
-- can be granted it later via the Roles & Permissions UI if they also want it.
INSERT INTO "RolePermission" ("RoleID", "PermissionID")
SELECT r."RoleID", p."PermissionID"
  FROM "Role"       r
  CROSS JOIN "Permission" p
 WHERE r."Name"     = 'Admin'
   AND r."TenantID" = 1
   AND p."Code"     = 'view_personal_dashboard'
ON CONFLICT DO NOTHING;
