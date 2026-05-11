# Litrix

Research analytics platform for Al-Baha University · College of Computing & Information Technology.

## Stack

- **Backend**: Django 5 + DRF + PostgreSQL
- **Frontend**: Angular 19 + Tailwind CSS
- **Hosting**: Vercel (frontend) · Render (backend) · Neon (database)

## Project Structure

```
litrix/
├── scrapers/                      Data acquisition
│   ├── scholar.py                 Google Scholar (canonical attribution)
│   ├── orcid.py                   ORCID + OpenAlex multi-strategy
│   └── manual.py                  Curated researcher profiles
├── classification/                Journal Q-classification
│   ├── classify.py                End-to-end pipeline
│   └── scimago_import.py          Bulk Scimago CSV import
├── citations/                     Citation backfill
│   ├── researcher.py              Author-level (Scholar graph)
│   └── per_paper.py               Per-paper (OpenAlex DOI/title)
├── tools/                         Utilities
│   ├── rescrape.py                Re-sync 0-paper researchers
│   ├── verify_attributions.py     Detect cross-author contamination
│   └── consolidate_journals.py    Merge duplicate Journal rows
├── backend/                       Django app
├── frontend/                      Angular app
├── .env                           Configuration (gitignored)
├── README.md
└── OPERATIONS_LOG.md              Full architectural history
```

## Setup

```bash
# 1. Environment
cp .env.example .env
# Fill DATABASE_URL, SERP_API_KEY

# 2. Backend
cd backend
pip install -r requirements.txt
python manage.py runserver

# 3. Frontend
cd frontend
npm install
ng serve
```

## Common Workflows

### Add a new researcher

```bash
# Has Google Scholar profile
python scrapers/scholar.py <scholar_id> <user_id>

# Has ORCID only
python scrapers/orcid.py --orcid <ORCID> --user <user_id>

# Manual entry (edit RESEARCHERS dict in manual.py first)
python scrapers/manual.py --uid <user_id>
```

### After any data import

```bash
# Classify journals (Q1/Q2/Q3/Q4)
python classification/classify.py

# Refresh citation graphs from Scholar
python citations/researcher.py
```

### Bulk operations

```bash
# Re-import latest Scimago rankings
python classification/scimago_import.py "scimagojr2025.csv"

# Re-sync researchers with 0 papers
python tools/rescrape.py

# Validate Scholar attribution (catches cross-contamination)
python tools/verify_attributions.py
```

## Architectural Principles

1. **Source of truth**: Scholar's `articles[]` (via Scholar_ID) for attribution. OpenAlex/CrossRef/ORCID for enrichment only.
2. **Deterministic identifiers**: Scholar_ID, DOI, ORCID. Never name-based fuzzy matching for cross-attribution.
3. **NormalizedName** for journals: strip vol/issue/year noise, expand abbreviations, drop stopwords.
4. **`Researcher.CitationsByYear`** for the dashboard (Scholar's author-level graph), not per-paper sums.
5. **Idempotent operations**: every script safe to re-run. SAVEPOINT + ROLLBACK for unique constraints.

## Data Sources

| Source | Used for | Cost |
|---|---|---|
| Google Scholar (SerpAPI) | Paper attribution + author graph | 1 credit/researcher |
| OpenAlex | DOI, ISSN, journal name, per-paper counts | Free |
| ORCID Public API | Self-reported works fallback | Free |
| Scimago | Q1-Q4 rankings (CSV download) | Free |

## See Also

- `OPERATIONS_LOG.md` — full architectural history with code snippets and trade-off analyses
