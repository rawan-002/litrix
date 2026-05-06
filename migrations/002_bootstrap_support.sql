


BEGIN;


CREATE UNIQUE INDEX IF NOT EXISTS "uq_college_name"
    ON "College" ("CollegeName");


CREATE UNIQUE INDEX IF NOT EXISTS "uq_department_name_college"
    ON "Department" ("DepartmentName", "CollegeID");


CREATE UNIQUE INDEX IF NOT EXISTS "uq_users_fullname_ar"
    ON "Users" ("FullName_Ar");


CREATE UNIQUE INDEX IF NOT EXISTS "uq_works_in_user_dept"
    ON "Works_In" ("UserID", "DepartmentID");


CREATE INDEX IF NOT EXISTS "ix_researcher_scopus_id"
    ON "Researcher" ("Scopus_ID")
    WHERE "Scopus_ID" IS NOT NULL;


CREATE UNIQUE INDEX IF NOT EXISTS "uq_researcher_orcid_id"
    ON "Researcher" ("ORCID_ID");

COMMIT;


