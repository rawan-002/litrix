# Litrix — Data Policy (Affiliation Contract)

> The single source of truth for **how a publication is counted** on Litrix.
> The rule lives here, *outside* the code, so it survives team turnover: any
> change to SQL, an API, an export, or a dashboard is reviewed against this
> document — not against one developer's memory of "how it works".
>
> Three layers keep this honest:
> **Policy** (this file) → **Implementation** (`verified_affil_clause` /
> `active_affil_clause`) → **Enforcement** (`backend/tools/integration_check.py`
> + the `deploy.ps1` gate).
>
> Last reviewed: 2026-08-02.

---

## 1. Purpose

Litrix reports the research output of Al-Baha University. The Google Scholar
scraper pulls **every** paper off a researcher's profile — including work they
published *before* joining Al-Baha (PhD/postdoc/visiting positions elsewhere).
Counting those as Al-Baha output would overstate the institution's numbers.

This policy exists so that:

- Official figures are **defensible to an accreditation body (NCAAA)** — every
  counted paper has *evidence* of an Al-Baha affiliation.
- Unverified papers are **visible, not hidden**, and never silently inflate a
  headline.
- The counting rule is **explicit and enforced**, so it can't drift as the code
  or the team changes.

---

## 2. `AffiliationVerified` semantics (tri-state)

Set only by the affiliation verifier (`backend/affiliation_verifier.py`, v3.0.0,
§8) using deterministic evidence — **never** by name matching.

| Value | Meaning | Official? |
|-------|---------|-----------|
| `TRUE`  | **Confirmed Al-Baha** — an authoritative or complete source shows an Al-Baha affiliation. | **Yes** |
| `FALSE` | **Confirmed NOT Al-Baha** — authored under another institution. | No |
| `NULL`  | **Pending review** — not yet verified. UNKNOWN, not evidence of anything. | No |

**Core principle: `NULL` is *unknown*, not *Al-Baha*.** Counting `NULL` as
Al-Baha inflates the numbers without evidence and is not defensible. Official
figures are **confirmed-only (`TRUE`)**.

---

## 3. Official numbers vs Working set

Two distinct purposes → two distinct filters. Never use one for the other.

- **Official number** — anything that states "the university/department/
  researcher produced N papers/citations/Q1s": every KPI, its drill-down, and
  the official export. **Confirmed-only (`TRUE`).**
- **Working set** — a list whose job is to *surface content*, not to state a
  count: e.g. Top Papers. Keeps `TRUE` + `NULL` so unverified papers stay
  visible **with a "Pending review" badge**, and drops only `FALSE`.
- **Operational workflow** — the verification/reconciliation queue. Shows **all
  three states** because it is a triage tool, not a report.

The two implementation helpers (`backend/analytics/stats.py`), both return `''`
when the Al-Baha toggle is OFF:

| Helper | Predicate (toggle ON) |
|--------|-----------------------|
| `verified_affil_clause(albaha_only, alias)` | `AND alias."AffiliationVerified" = TRUE` |
| `active_affil_clause(albaha_only, alias)`   | `AND alias."AffiliationVerified" IS DISTINCT FROM FALSE` (TRUE + NULL) |

---

## 4. Component matrix

| Component | Filter | Reason |
|-----------|--------|--------|
| Overview KPIs (papers / citations / Q1–Q4 / Scopus / ISI) | `TRUE` | Official statistics |
| Department KPIs & cards | `TRUE` | Official statistics |
| Researcher leaderboard | `TRUE` | Official statistics |
| KPI drill-down (`classified_papers`) | `TRUE` | KPI invariant (click == rows) |
| Excel export | `TRUE` | Official export (matches the screen) |
| Public dashboard (`/api/public/*`) | `TRUE` | Official, public-facing |
| Top Papers | `TRUE` + `NULL` | Highlight list (badged, not a count) |
| Verification / reconciliation queue | All states | Operational workflow |

**Pending-review indicator** — `overview()` (auth) and public `overview()` both
return `pending_review = COUNT(AffiliationVerified IS NULL)` for the scope,
computed *without* the affiliation filter and shown as its own strip. The
backlog is visible, never folded into a headline.

---

## 5. Invariants (MUST NOT break)

1. **Overview KPI == Drill-down rows == Excel value == API == SQL** — for the
   same scope, the Publications KPI equals the drill-down row count, the Excel
   Overview sheet value, the API total, and a raw
   `SQL COUNT(AffiliationVerified = TRUE)`.
2. **`NULL` is never counted as Al-Baha** in any official number.
3. **Top Papers under `affiliation=albaha` contains no `FALSE` row**, and every
   row exposes `affiliation_verified` so a `NULL` can be badged.
4. **Monotonicity:** `albaha <= all` for every official metric (papers,
   citations, Q1–Q4). A violation means some query uses the wrong helper.
5. **Excel is compared sheet-by-sheet to its feeding endpoint** — never by the
   file's total row count (the workbook is a multi-sheet summary, not a paper
   list).

---

## 6. Regression guard (enforcement)

Two things keep the code from silently violating this policy:

- **`backend/tools/integration_check.py`** — read-only integration test (13
  assertions covering §5). Exit 1 on any violation. Run any time:
  ```bash
  cd backend && python tools/integration_check.py
  ```
- **`deploy.ps1`** — runs the guard as a mandatory pre-flight step
  (backend check → **integration guard** → frontend build). A broken data
  contract stops the deploy before anything reaches the remote.

So a developer who reintroduces `IS DISTINCT FROM FALSE` in an official query, or
points a KPI at `active_affil_clause`, fails the gate — the deviation is caught
before production, not after.

---

## 7. Rules of thumb for new code

- Official number / count / KPI / export? → `verified_affil_clause`.
- Working / highlight list that should still surface unverified papers? →
  `active_affil_clause`, and expose `affiliation_verified` for a badge. Not a
  count.
- Triage / verification tool? → no affiliation filter (all states).
- Never reintroduce a single shared clause for both meanings; never count `NULL`
  as Al-Baha; run `integration_check.py` after the change (the gate will too).

---

## 8. How a paper gets its state (the verifier)

`backend/affiliation_verifier.py` (v3.0.0) sets `TRUE` / `FALSE` / `NULL` with an
**authority model** — never name matching:

- **Authoritative (decisive):** publisher landing-page HTML, article PDF, IEEE
  rendered page. Can return a final `TRUE` or `FALSE`.
- **Supporting (fallback):** OpenAlex, Crossref. Decide only on **complete**
  affiliation data; a supporting negative is never a final `FALSE`.
- Missing data / API failure → stays `NULL` (retryable); never downgraded to
  `FALSE` on absence of evidence.
- A mandatory guard: an inconclusive re-run **never** overwrites an existing
  `TRUE`/`FALSE` verdict with `NULL`.

As of the 2026-08-02 full-database pass: **924 TRUE / 684 FALSE / 389 NULL**
(of 1997). Of the 389 NULL, ~333 have no DOI (unverifiable by this tool — a
separate DOI-recovery project) and ~56 have a DOI but stayed inconclusive
(paywalled / bot-walled publishers). Reducing the NULL backlog is a data-quality
task — **not** a reason to count `NULL` as Al-Baha.
