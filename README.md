# Litrix

Research analytics platform for **Al-Baha University · College of Computing & Information Technology**.
Bilingual (Arabic/English). It ingests each researcher's publications from Google
Scholar (and enrichment sources), classifies journals Q1–Q4, tracks citations, and
serves dashboards + per-researcher profiles — plus a no-login public dashboard.

- **Backend:** Django 5 + Django REST Framework, PostgreSQL
- **Frontend:** Angular 21 (standalone components + signals), Tailwind, Chart.js + d3
- **Hosting:** Vercel (frontend) · Render (backend) · Neon (PostgreSQL)
- **Data pipeline:** standalone Python scripts (psycopg2) — scrapers, classification, citations, tools

---

## 1. Repository structure

The repo is split into exactly two parts — **`frontend/`** and **`backend/`** — plus
root-level configuration. All Python (Django **and** the data pipeline) lives under
`backend/`.

```
litrix/
├── frontend/                     Angular 21 SPA  → deployed to Vercel
│   └── src/app/
│       ├── core/                 AuthService, guards, interceptors, AffiliationService
│       ├── shared/               Layout (sidebar shell), icon set, shared UI
│       ├── components/           Researcher profile, citations chart, paper modal, …
│       ├── pages/                Search, settings, departments, network, admin/*
│       └── public/               No-login public dashboard + public profiles (/api/public/*)
│
├── backend/                      Django 5 + DRF  → deployed to Render. Holds ALL Python.
│   ├── litrix_backend/           Django project (settings.py, urls.py, wsgi.py)
│   ├── accounts/                 Custom User, JWT auth, registration/invitation, RBAC  → /api/auth/
│   ├── analytics/                Dashboard/reporting endpoints  → /api/   (+ public_views → /api/public/*)
│   ├── migrations/               Raw-SQL domain-schema migrations (NOT Django migrations)
│   ├── scrapers/                 Data acquisition: scholar.py · orcid.py · manual.py
│   ├── citations/                Citation backfill: researcher.py (author graph) · per_paper.py
│   ├── classification/           Journal Q1–Q4: classify.py · scimago_import.py
│   ├── tools/                    Ops: integrity_check.py · verify_attributions.py · rescrape.py · run_migration.py · …
│   ├── litrix_db.py              Shared psycopg2 connection helper — `from litrix_db import db`
│   ├── litrix_schema.dbml        Live ER diagram (paste into dbdiagram.io)
│   ├── Dockerfile · build.sh     Render / Cloud Run build
│   ├── affiliation_verifier.py   Al-Baha affiliation verification (own Django-aware db)
│   └── requirements.txt
│
├── .github/workflows/            ci.yml (build + check on push) · integrity.yml (daily DB health)
├── deploy.ps1                    One-shot: local gate (build + check) → commit → push
├── .env                          Local config — gitignored (DB creds, API keys)
├── .env.example                  Template (no secrets)
└── README.md                     ← you are here
```

> Pipeline scripts use a 2-line `sys.path` bootstrap so `from litrix_db import db`
> resolves to `backend/litrix_db.py`. Run them from `backend/` (see §6).

---

## 2. Architecture — two write paths, one database

The defining design decision: **Django does NOT own the domain schema.**

1. **Standalone Python scripts** (`backend/scrapers`, `backend/citations`,
   `backend/classification`, `backend/tools`, and the loose `backend/*.py`
   one-off scripts) connect directly via `psycopg2` and are the **only writers**
   of the domain tables (`Users`, `Researcher`, `ResearchPaper`, `Authors`,
   `Journals`, `JournalRankings`, `ExternalAuthors`, …). They share one helper:
   `from litrix_db import db`.
2. **The Django backend** maps those same tables with `managed = False` models
   (`backend/analytics/models.py`, `backend/accounts/models.py`).
   `python manage.py migrate` only touches Django's own auth/admin/JWT tables.
   Domain schema changes are raw SQL files in `backend/migrations/`, applied with
   `python tools/run_migration.py backend/migrations/<file>.sql`.

All SQL identifiers are quoted **PascalCase**: `"ResearchPaper"."PaperID"`.

**Environment switch:** `DATABASE_URL` in the root `.env` is the single switch.
Set → production (Neon); empty → local Postgres via discrete `DB_*` vars. This
applies to Django **and** every pipeline script — always know which database a
script will hit before running it.

---

## 3. Data-attribution rules (non-negotiable)

These exist because name-based matching once cross-contaminated **602 papers**
between researchers with similar names (full forensic history was in the legacy
`OPERATIONS_LOG.md`; the short version below).

1. **Scholar's `articles[]` (via `Scholar_ID`) is the single source of truth** for
   paper↔researcher attribution. OpenAlex/CrossRef/ORCID are **enrichment only**
   (DOI, ISSN, journal names, per-paper citation counts).
2. **Deterministic identifiers only** for matching: `Scholar_ID`, `DOI`, `ORCID`,
   OpenAlex Author ID. **Never** name-based fuzzy matching for cross-attribution.
3. **`Researcher.CitationsByYear`** (Scholar's author-level graph) feeds the
   dashboard — not per-paper sums.
4. **Every script must be idempotent** — safe to re-run. Use
   `INSERT … ON CONFLICT`, `SAVEPOINT` + `ROLLBACK`, `COALESCE` instead of
   overwrites. Destructive scripts require a `--confirm` flag.
5. Journals dedupe via `NormalizedName` (strip vol/issue/year noise, expand
   abbreviations, drop stopwords).

**The 602-paper incident (lesson learned):** an early version matched papers to
researchers by name. Two researchers with similar Arabic names ended up sharing
602 papers. The fix: attribution flows ONLY from Scholar's per-author `articles[]`
keyed on `Scholar_ID`, with a verification pass (`tools/verify_attributions.py`)
that detects and removes cross-author contamination.

---

## 4. Database (Neon PostgreSQL)

32 domain tables, FK-enforced. The live ER diagram is `backend/litrix_schema.dbml`
(paste into [dbdiagram.io](https://dbdiagram.io); regenerate after schema changes).

### Table groups

| Group | Tables |
|---|---|
| **Tenancy & identity** | `Tenant`, `Users`, `Researcher` |
| **Org structure** | `College`, `Department`, `Works_In` (researcher position, M:N) |
| **Research output** | `ResearchPaper`, `Authors` (paper↔user, M:N), `ExternalAuthors`, `Keywords`, `PaperKeywords` |
| **Journals & ranking** | `Journals`, `JournalRankings`, `ISSN_Mapping` |
| **Auth / RBAC** | `Role`, `Permission`, `RolePermission`, `Invitation`, `RegistrationRequest`, `EmailVerification`, `RefreshToken` |
| **Reporting campaigns** | `ReportCampaign`, `ReportSubmission`, `ReportPaperDecision` |
| **Ops** | `Notification`, `ScheduledNotification`, `SyncJob`, `AuditLog`, `AuthorReviewQueue` |

### Canonical columns — use these, ignore the rest

- **Paper↔researcher attribution:** ONLY via `Authors`, keyed on deterministic IDs
  (`Users."Scholar_ID"`, `"Orcid_ID"`). Never name-based fuzzy matching.
- **Citations (per-period, dashboards):** `ResearchPaper."CitationsByYear"` and
  `Researcher."CitationsByYear"` (per-year JSON, keyed by *year received*).
  Lifetime total = `ResearchPaper."RawData_Log"->'cited_by'`.
- **Affiliation filter:** `ResearchPaper."AffiliationVerified"` (TRUE / NULL =
  include, FALSE = authored elsewhere). Written by `backend/affiliation_verifier.py`.
- **Research interests:** `Researcher."ResearchInterests"` (jsonb array of Scholar
  "areas of interest"; shown on the profile bio).
- **Profile photo:** `Users."PhotoURL"` (Scholar thumbnail, or a researcher-uploaded
  data-URI from Settings).
- **Identity IDs:** `Users` holds the **matching** anchors (`Scholar_ID`, `Orcid_ID`,
  `Scopus_ID` — UNIQUE, drive attribution). `Researcher` holds **display** copies.

### Dedup / natural keys

- **`ResearchPaper."DOI"` — `uq_paper_doi`** (partial unique `WHERE DOI IS NOT NULL`):
  strongest guard against duplicate papers; DOI-less papers fall back to title dedup.
- **`ResearchPaper."NormalizedTitle"` — `uq_paper_normalized_title`** (partial unique):
  title-based fallback for DOI-less papers.
- **`Authors (UserID, PaperID)` — `uq_authors_user_paper`** (unique): blocks
  double-linking one researcher to one paper. External authors carry `UserID = NULL`
  + `AuthorNameRaw` and never collide.

### Gotchas

- Duplicate identity columns (`Users` vs `Researcher`) are deliberate — `Users`
  copies are load-bearing (unique constraints + matching). Don't "consolidate".
- `Citations` / `CitationsHistory` tables are unused; the live source is
  `CitationsByYear`.
- FK columns aren't auto-indexed; hot ones are in `backend/migrations/20260611_perf_indexes.sql`.

---

## 5. Local setup

```powershell
# 0. Config — copy the template and fill DATABASE_URL (or DB_* for local) + SERP_API_KEY
cp .env.example .env

# 1. Backend  (Django dev server → http://localhost:8000)
cd backend
pip install -r requirements.txt
python manage.py runserver

# 2. Frontend (ng serve → http://localhost:4200)
cd frontend
npm install
npm start
```

`npm run build` (frontend) runs the **strict** TypeScript compiler — this is where
type errors surface. Always build before pushing; `ng serve` is more lenient.

---

## 6. Data pipeline (run from `backend/`)

```bash
cd backend

# Ingest a researcher's papers (Google Scholar — canonical attribution)
python scrapers/scholar.py <scholar_id> <user_id>

# ORCID / OpenAlex fallback (no Scholar profile)
python scrapers/orcid.py --orcid <ORCID> --user <user_id>

# Classify journals Q1–Q4  (run after ANY import)
python classification/classify.py

# Refresh Scholar citation graphs (+ profile photos, --missing-photos = cheap)
python citations/researcher.py
python citations/researcher.py --missing-photos --yes

# Detect + DELETE cross-author contamination (SerpAPI, destructive)
python tools/verify_attributions.py

# Read-only health check (safe; CI runs it daily)
python tools/integrity_check.py

# Apply a raw-SQL domain-schema migration
python tools/run_migration.py backend/migrations/<file>.sql
```

**Principles:** Scholar `articles[]` for attribution; deterministic IDs only;
`Researcher.CitationsByYear` for dashboards; every script idempotent (re-runnable).

| Source | Used for | Cost |
|---|---|---|
| Google Scholar (SerpAPI) | Paper attribution + author graph + photo | 1 credit/researcher |
| OpenAlex | DOI, ISSN, journal name, per-paper counts | Free |
| ORCID Public API | Self-reported works fallback | Free |
| Scimago | Q1–Q4 rankings (CSV) | Free |

---

## 7. Deploy

```powershell
# From repo root. Runs a local gate (frontend build + manage.py check),
# then commits + pushes. GitHub Actions re-runs both; Render + Vercel auto-build.
.\deploy.ps1 "feat: message"          # add -SkipChecks to bypass the local gate
```

Pipeline (auto on push to `main`):

```
Vercel (Angular, root=frontend)  ──►  Render (Django, root=backend)  ──►  Neon (Postgres)
   ng build → static CDN              build.sh → migrate → gunicorn        managed DB + SSL
```

**Render** (root directory `backend`): build `./build.sh`, start
`gunicorn litrix_backend.wsgi:application`. Env vars: `DATABASE_URL`,
`DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS`,
`CORS_ALLOWED_ORIGINS`, `PYTHON_VERSION=3.12.3`.
Free tier sleeps after 15 min idle (first request ~30–60 s); a Docker build can
take ~30 min.

**Vercel** (root directory `frontend`): build `npm run build`, output
`dist/frontend/browser`. The repo is kept **public** so Hobby-plan deploys aren't
blocked by an author/owner mismatch.

**Neon:** the single managed Postgres; `DATABASE_URL` points every component at it.

**Troubleshooting:** CORS error → `CORS_ALLOWED_ORIGINS` must include the Vercel
domain (no trailing slash). 502 → backend asleep, wait ~30 s. Prod TS build fails
but `ng serve` passed → run `npm run build` locally (strict mode, esp. d3 typing).

---

## 8. Roles & conventions

- **RBAC:** named permissions (`manage_users`, `trigger_sync`, …) checked by
  `backend/accounts/permissions.py` and `permissionGuard(...)` in
  `frontend/.../app.routes.ts` — keep both sides in sync.
- **Canonical profile URL:** `/profile/Lit-NNNNNN` (`litrix_id`); `/researcher/:id`
  is a legacy alias. Public profiles live at `/public/researcher/:litrix_id`.
- **Global Al-Baha filter:** one header toggle (`AffiliationService`, default ON,
  persisted to localStorage) drives every dashboard, the profile KPIs, the
  citations chart, and the co-authors list.
- **Citations metric:** dashboards use per-period per-paper; the public site and
  researcher profile show lifetime, by design.

---

## 9. GCP migration (planned)

A migration from Vercel/Render/Neon to **GCP `me-central2` (Dammam)** — Cloud Run +
Cloud SQL + Firebase Hosting — is planned. `backend/Dockerfile` and
`frontend/firebase.json` belong to it. The Dockerfile is a 2-stage build
(builder compiles psycopg2 wheels; slim runtime ~180 MB, non-root user, Whitenoise
static).

---

## 10. Notes

- The repo lives on an Arabic-named OneDrive path on Windows; OneDrive sometimes
  leaves stale `.git/index.lock` files — `deploy.ps1` clears them.
- Secrets (`.env`, `client_secret*.json`, `token.json`) are gitignored — never commit.
- Pipeline scripts wrap stdout in a UTF-8 `TextIOWrapper` for Arabic console output —
  keep that pattern in new scripts.
