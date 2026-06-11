# Litrix — Database Guide (for developers)

PostgreSQL on **Neon**. 32 domain tables, 52 enforced foreign keys.
The full ER diagram lives in [`litrix_schema.dbml`](./litrix_schema.dbml) —
paste it into [dbdiagram.io](https://dbdiagram.io) to view/edit. It's
auto-generated from the live schema, so regenerate it after any schema change.

> **Two write paths (read this first):** Django does **not** own the domain
> schema. Standalone `psycopg2` scripts (`scrapers/`, `citations/`,
> `classification/`, `tools/`, `backend/*.py`) are the only writers of the
> domain tables and connect via `from litrix_db import db`. Django maps the
> same tables with `managed = False` models. Domain schema changes are raw SQL
> in `backend/migrations/*.sql`, applied with `tools/run_migration.py` —
> **never** `manage.py makemigrations` for domain tables. All identifiers are
> quoted PascalCase: `"ResearchPaper"."PaperID"`.

## Table groups

| Group | Tables |
|---|---|
| **Tenancy & identity** | `Tenant`, `Users`, `Researcher` |
| **Org structure** | `College`, `Department`, `Works_In` (a researcher's position, M:N) |
| **Research output** | `ResearchPaper`, `Authors` (paper↔user, M:N), `ExternalAuthors`, `Keywords`, `PaperKeywords` |
| **Journals & ranking** | `Journals`, `JournalRankings`, `ISSN_Mapping` |
| **Auth / RBAC** | `Role`, `Permission`, `RolePermission`, `Invitation`, `RegistrationRequest`, `EmailVerification`, `RefreshToken` |
| **Reporting campaigns** | `ReportCampaign`, `ReportSubmission`, `ReportPaperDecision` |
| **Ops** | `Notification`, `ScheduledNotification`, `SyncJob`, `AuditLog`, `AuthorReviewQueue` |
| **Import staging** | `Scimago_Staging` (raw CSV dump — not a domain entity) |

## Canonical columns — use these, ignore the rest

- **Paper↔researcher attribution:** ONLY via `Authors` keyed on deterministic
  IDs (`Users."Scholar_ID"`, `"Orcid_ID"`). Never name-based fuzzy matching
  (it once cross-contaminated 602 papers — see `OPERATIONS_LOG.md`).
- **Citations (this-period, dashboards):** `ResearchPaper."CitationsByYear"`
  and `Researcher."CitationsByYear"` (per-year JSON, keyed by *year received*).
  The app expresses this through one helper — `_cites_expr()` in
  `analytics/views.py`. Lifetime total = `ResearchPaper."RawData_Log"->'cited_by'`.
- **Affiliation filter:** `ResearchPaper."AffiliationVerified"`
  (TRUE / NULL = include, FALSE = authored elsewhere). Written by
  `affiliation_verifier.py`.
- **Identity IDs:** `Users` holds the **matching** anchors (`Scholar_ID`,
  `Orcid_ID`, `Scopus_ID` — these have UNIQUE constraints and drive the
  attribution pipeline). `Researcher` holds the **display** copies
  (`ORCID_ID`, `Scopus_ID`, `OpenAlex_AuthorID`).

## Dedup / natural keys (what stops duplicates)

- **`ResearchPaper."DOI"` — `uq_paper_doi`**, partial unique
  (`WHERE DOI IS NOT NULL`). The strongest guard against duplicate papers;
  DOI-less papers (NULL) are allowed to repeat and fall back to title dedup.
  A plain `UNIQUE(DOI)` (`ResearchPaper_DOI_key`) also exists and is redundant
  with the partial index. Uniqueness is currently **global**, not per-tenant
  (single tenant today); revisit `(TenantID, DOI)` only if Litrix goes
  multi-institution.
- **`ResearchPaper."NormalizedTitle"` — `uq_paper_normalized_title`**, partial
  unique. Title-based fallback for the DOI-less papers. (`Title` also has a
  full `UNIQUE`.)
- **`Authors (UserID, PaperID)` — `uq_authors_user_paper`**, unique. The PK is
  the surrogate `AuthorLinkID`, so *this* is what blocks double-linking one
  researcher to one paper after disambiguation. External/unmatched authors
  carry `UserID = NULL` and never collide (NULLs are distinct in a btree
  unique), which is why it isn't written as a partial index.

## Known debt / gotchas

- **Duplicate identity columns** (`Users` vs `Researcher` ORCID/Scopus; plus a
  legacy `Users."ORCID"`). Kept deliberately — `Users` copies are load-bearing
  (unique constraints + matching). No drift today; don't "consolidate" without
  rewriting the attribution pipeline.
- **`Citations` / `CitationsHistory` tables are empty/unused.** Reserved for an
  optional per-paper snapshot (`refresh_hybrid.py --write-citations-table`);
  the live citation source is `CitationsByYear`. Don't read them.
- **External authors live two ways:** the `ExternalAuthors` table *and*
  `Authors` rows with a NULL `UserID` + `AuthorNameRaw`.
- **Performance:** FK columns aren't auto-indexed; the hot join/filter ones are
  indexed in `backend/migrations/20260611_perf_indexes.sql`. Add an index when
  you add a frequently-joined/filtered column.
