# Litrix — Complete Operations Log
**College of Computing & Information Technology · Research Analytics**

التوثيق الكامل المفصّل لكل العمليات، الـ scripts، القرارات المعمارية، والـ trade-offs اللي اتخذناها لتنظيف وتحسين بيانات الـ Litrix dashboard.

---

# Part I — الإعداد العام

## 0. الـ Stack المعماري

### Backend
- **Django 5.0.6** + Django REST Framework
- **PostgreSQL** (Neon production + local development)
- **Python 3.12** للـ scraping scripts

### Frontend
- **Angular 19** standalone components
- **Tailwind CSS** + Apple-style minimalism
- **Signals** للـ state management
- **Vercel** للـ hosting

### Hosting
- **Vercel** — frontend (https://litrix.vercel.app)
- **Render** — Django backend
- **Neon** — production PostgreSQL (`ep-fragrant-violet-alciwd6u.c-3.eu-central-1.aws.neon.tech`)

### Data Sources
| Source | Endpoint | الاستخدام |
|---|---|---|
| Google Scholar | SerpAPI `google_scholar_author` | Paper attribution + author cited_by graph |
| OpenAlex | `api.openalex.org/works` | DOI/ISSN/journal enrichment + per-paper counts_by_year |
| ORCID | `pub.orcid.org/v3.0` | Self-reported works |
| CrossRef | `api.crossref.org/works` | Fallback enrichment |
| Scimago | CSV download from scimagojr.com | Q1/Q2/Q3/Q4 rankings |

---

## 1. الـ Architectural Principles

أربع قواعد أساسية تحكم كل قرار في النظام:

### Rule 1: Source of Truth Hierarchy
```
Scholar's articles[] (via Scholar_ID) = SINGLE source of truth for attribution
OpenAlex/CrossRef/ORCID = enrichment only (DOI, ISSN, journal name, citations)
```
**ليش**: name-based matching يخلط بين باحثين بنفس الاسم (T Alghamdi vs تغريد الغامدي).

### Rule 2: Deterministic Identifiers Only
لما نسحب أو نطابق، نستخدم فقط الـ identifiers الـ deterministic:
- Scholar_ID
- DOI
- ORCID
- OpenAlex Author ID

**Never**: name search بدون validation.

### Rule 3: Single Switch للـ Environment
`DATABASE_URL` في `.env` = source of truth. لو set → Neon. لو فاضي → Local.

### Rule 4: Idempotent Operations
كل script يقدر يُشغّل مرات متعددة بدون يكسر البيانات. Use `INSERT ... ON CONFLICT`، `SAVEPOINT`، `COALESCE` بدل overwrites.

---

# Part II — تنظيف البيانات

## 2. Cross-Author Contamination (الأهم)

### المشكلة الأصلية

كان عندنا scenario خطير:
- باحث `تغريد الغامدي` (Tagreed Alghamdi) في DB
- ورقة `T Alghamdi - Some Paper Title` ظهرت في profile
- لكن الـ `T` تخص `طارق الغامدي` (Tariq Alghamdi)، باحث ثاني!

السبب: الـ enrichment script كان يبحث في OpenAlex بـ author name، ويربط أي ورقة تطابق `T Alghamdi`.

### الـ Forensic Investigation

شغّلنا queries للتأكد:
```sql
SELECT a."UserID", u."FullName_Ar", rp."Title", rp."RawData_Log"->>'authors'
FROM "Authors" a
JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
JOIN "Users" u ON u."UserID" = a."UserID"
WHERE rp."Title" ILIKE '%some signature title%';
```

النتيجة: 602 ورقة كانت linked لباحثين خطأ.

### الـ Architectural Fix

**`verify_all_researchers.py`** — Cleanup script:

```python
def main():
    # لكل باحث عنده Scholar_ID
    for uid, name, scholar_id in get_all_researchers():
        # 1. اسحب Scholar's verified articles[]
        verified_titles = fetch_scholar_titles(scholar_id)
        normalized = {normalize_title(t) for t in verified_titles}

        # 2. لقّى الأوراق المربوطة بهذا الباحث في DB
        cur.execute('''
            SELECT a."AuthorID", rp."PaperID", rp."Title"
            FROM "Authors" a
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            WHERE a."UserID" = %s
        ''', [uid])

        # 3. احذف الـ links لأوراق مش في Scholar profile
        for author_id, paper_id, title in cur.fetchall():
            if normalize_title(title) not in normalized:
                cur.execute('DELETE FROM "Authors" WHERE "AuthorID" = %s', [author_id])
                deleted += 1
```

### النتيجة
- **602 Authors links خاطئة** اتحذفت
- 100% Scholar-ID-based attribution
- Cross-contamination منع تماماً

### Lesson Learned
أي pipeline يستخدم name search لازم يكون فيه **dual validation**:
1. Title exact match
2. Author last-name overlap (لكن مع ID-based attribution كـ source of truth)

---

## 3. Wipe & Reset Strategy

### المشكلة
لما تكون البيانات contaminated، أحياناً الأنظف نمسح ونعيد.

### `wipe_scholar_papers.py`

```python
def main(confirm=False):
    if not confirm:
        print("Add --confirm to actually delete")
        return

    cur.execute('DELETE FROM "Authors" WHERE ...')
    cur.execute('DELETE FROM "ExternalAuthors" WHERE ...')
    cur.execute('''
        DELETE FROM "ResearchPaper"
        WHERE "Source" IN ('Scholar', 'OpenAlex', 'Both')
    ''')
```

### المثال العملي — Almashlah
لما اكتشفنا papers طبية في profile الباحث (UID 81):
```cmd
:: 1. حذف Authors links
DELETE FROM "Authors" WHERE "UserID" = 81;

:: 2. حذف orphan papers (الي ما عاد عندهم authors)
DELETE FROM "ResearchPaper"
WHERE "PaperID" NOT IN (SELECT DISTINCT "PaperID" FROM "Authors");

:: 3. إعادة سحب من Scholar بالـ ID
python sync_researcher.py Gtbbx1YAAAAJ 81
```

### Critical Insight
الـ wipe يحذف الـ `CitationsByYear` JSONB من الـ orphan papers. هذي بيانات قيّمة — احتفظ بنسخة قبل أي wipe.

---

# Part III — Scraping Architecture

## 4. الـ Canonical Scraper: `sync_researcher.py`

### Design Goals
1. Robust pagination (يجيب كل الـ papers، مهما كان عددها)
2. Safe enrichment (ما يربط false positives)
3. Resume-friendly (يقدر يكمّل لو انقطع)

### الـ Flow الكامل
```
[Scholar API] → 3-pass fetch → all_articles dict (deduped by cites_id)
    ↓
For each article:
    [Title normalize] → ResearchPaper insert/find
    [OpenAlex DOI lookup] → enrichment (DOI, ISSN, journal name)
    [Author validation] → confirm match
    [JournalID lookup] → link to ranking
    [Authors link] → bind to UserID
    ↓
[Researcher.CitationsByYear] ← Scholar's cited_by.graph
```

### الـ 3-pass Pagination

**ليش 3 passes**: Scholar's API يرجّع نتائج مختلفة بناءً على الـ sort order، فبدمجهم نضمن coverage كامل.

```python
def fetch_scholar_profile(scholar_id):
    all_articles = {}
    cited_by_graph = []
    for sort in [None, "pubdate", "title"]:
        start = 0
        empty_streak = 0
        pages = 0
        while True:
            articles, raw = _serp_page(scholar_id, start, sort=sort)
            if not cited_by_graph and raw:
                cb = raw.get("cited_by") or {}
                cited_by_graph = cb.get("graph") or []
            if not articles:
                empty_streak += 1
                if empty_streak >= 3: break
                start += 100
                continue
            for a in articles:
                # Dedup priority: cites_id (stable) > link > normalized title
                key = (a.get("cites_id") or a.get("citation_id")
                       or a.get("link") or normalize_title(a.title))
                all_articles.setdefault(key, a)
            if len(articles) < 100: break
            start += 100
    return list(all_articles.values()), cited_by_graph
```

### Critical Bug Fix
كان dedup بـ `normalize_title` فقط، فالأوراق بعناوين متشابهة (caps/punctuation) كانت تتحسب واحدة. Switched لـ `cites_id` (stable per-paper).

### النتائج
- Almashlah: **708 papers** (كان 100 قبل الـ fix)
- 0 cross-attribution

---

## 5. ORCID-Based Scraping: `sync_by_orcid.py`

### المشكلة
3 باحثين بدون Scholar profile لكن عندهم ORCID:
- UID 9: ابتسام الدربي (`0000-0003-4781-1145`)
- UID 50: سعد القثامي (`0000-0002-2111-3456`)
- UID 92: محمد آل بنه (`0000-0001-6564-8340`)

### الـ 3-Strategy Cascade

#### Strategy 1: Direct ORCID filter
```python
GET /works?filter=author.orcid:ORCID&per-page=200
```
سريع وdeterministic لو OpenAlex عنده الـ ORCID مربوط بالـ works.

#### Strategy 2: Author lookup
```python
GET /authors/orcid:ORCID  → get author_id
GET /works?filter=author.id:author_id&per-page=200
```
يستخدم لو الأوراق ما عليها ORCID tag مباشرة لكن الـ author نفسه مسجّل.

#### Strategy 3: ORCID API direct
```python
GET https://pub.orcid.org/v3.0/{orcid}/works
→ extract DOIs (self-reported)
→ for each DOI: GET /works/doi:DOI on OpenAlex
```
الأكثر شمولاً (self-reported من الباحث نفسه)، لكن الأبطأ.

### مثال عملي — سعد القثامي

```
Pulling OpenAlex works for ORCID: 0000-0002-2111-3456
  [strategy 1] no works tagged with this ORCID — trying author lookup
  [strategy 2] found OpenAlex author A5033604188 — fetching works
  [strategy 3] falling back to ORCID API self-reported works
  [strategy 3] ORCID API returned 26 DOIs — fetching from OpenAlex
    fetched 10/26
    fetched 20/26
  [strategy 3] → 25 works enriched (of 26 DOIs)
```

من 46 work على ORCID profile، 25 عندهم DOI و OpenAlex لقاهم. الـ 21 الباقية بدون DOI = نفقدهم في الـ deterministic path.

### الـ Modes الإضافية

```cmd
:: للبحث في OpenAlex بالاسم + affiliation (للتحقق اليدوي)
python sync_by_orcid.py --search "Mohammed Khadran" --affiliation "Al-Baha"

:: بعد الـ verification، نسحب بالـ OpenAlex author ID
python sync_by_orcid.py --openalex-id A5012345678 --user 85
```

### Logic Safeguards
- `MappingCriteria='orcid_lookup'` للـ ORCID-based
- `MappingCriteria='openalex_author_id'` للـ search-based
- ما تخلط مع `'manual_bibtex'` (للـ Alomari) أو `'scholar_id_verified'` (الـ default)

---

## 6. Manual Paper Entry: `add_profile_papers.py`

### المشكلة
5 باحثين بدون Scholar/ORCID لكن عندهم profiles على ResearchGate/DBLP:

| UID | Researcher | Source | Papers |
|---|---|---|---|
| 24 | الترابي | DBLP | 2 |
| 47 | ريم الجوفي | ResearchGate | 3 |
| 85 | محمد خضران | ResearchGate | 2 |
| 86 | محمد القرشي | ResearchGate | 9 |
| 99 | ممدوح ميرغني | ResearchGate (mixed identity!) | 7 |

### Architecture
**Hardcoded papers list per researcher** + **3 matching strategies**:

```python
RESEARCHERS = {
    24: {
        "name": "Elturabi Osman Ahmed Habib",
        "lastname_validators": ["habib"],
        "papers": [
            {"title": "Sustainable Learning of Computer Programming...",
             "year": 2023,
             "journal": "Intelligent Automation and Soft Computing",
             "volume": "36", "issue": "2", "pages": "1687-1697"},
            ...
        ]
    },
    ...
}
```

### Per-Paper Logic

```python
def process_paper(paper_meta, researcher_info, manual=False):
    if manual:
        oa = None  # skip lookup
    else:
        # 1. Try OpenAlex by title
        oa = openalex_search(paper_meta["title"])
        if oa:
            # 2. Validate
            if jaccard(title, oa.title) < 0.65: oa = None
            elif abs(year - oa.year) > 2: oa = None
            elif not lastname_in(oa.authors, validators): oa = None

    if not oa and not manual:
        # 3. CrossRef fallback
        oa = crossref_search(...)

    # 4. Insert with appropriate Source
    src = 'OpenAlex' if oa else 'Manual'
    upsert_paper(...)
```

### الـ Validation Pipeline
1. **Title Jaccard ≥ 0.65** — token similarity (handles punctuation/word order)
2. **Year ±2** — preprint vs published tolerance
3. **Lastname overlap** — يمنع false positives (e.g., "Elturabi Habib" ≠ "Sara Habib")

### Critical Issue: Identity Collision
ممدوح ميرغني (UID 99) في ResearchGate كان فيه 9 papers من **3 أشخاص مختلفين**:
- Sudan IT researcher (الباحث الفعلي) — IoT, GIS papers
- Wolfgang Tress collaborator — Organic Solar Cells (physics)
- Mahmoud Aboughaly co-author — different Egyptian researcher

**الحل**: pre-filter يدوي — استبعدنا الـ Solar Cells papers من الـ config.

```python
99: {  # ممدوح ميرغني — Sudan/IT papers ONLY
    "name": "Mumdouh Mirghani Mohamed Hassan",
    "lastname_validators": ["mumdouh", "mamdouh", "hassan"],
    "papers": [
        # ✓ ITS, GIS, IoT papers (Sudan-based)
        {"title": "Using GIS to Find the Best Safe Route between Khartoum...", "year": 2023},
        # ✗ EXCLUDED: Solar Cells papers (physics, not our researcher)
    ]
},
```

### Rate Limit Handling
لما OpenAlex rate-limited:
```python
backoff = [10, 30, 60, 120, 180]  # seconds
for attempt in range(retries):
    try:
        r = httpx.get(url)
        if r.status_code == 429:
            wait = backoff[min(attempt, len(backoff)-1)]
            retry_after = r.headers.get("Retry-After")
            if retry_after.isdigit():
                wait = max(wait, min(int(retry_after), 300))
            time.sleep(wait)
            continue
        ...
```

### `--manual` Flag
لما OpenAlex/CrossRef مش متاح، نضع البيانات يدوياً بـ `Source='Manual'`:
```cmd
python add_profile_papers.py --uid 24 --manual
```

---

## 7. Rescrape Missing: `rescrape_missing.py`

### المشكلة
بعد parallel runs (4 cmd windows)، 37 باحث عندهم Scholar_ID لكن 0 papers بسبب SerpAPI rate limits.

### الـ Diagnostic Query
```sql
SELECT u."UserID", u."FullName_Ar", u."Scholar_ID"
FROM "Users" u
LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
WHERE u."Scholar_ID" IS NOT NULL AND u."Scholar_ID" <> ''
  AND u."UserType" = 'Researcher'
GROUP BY u."UserID", u."FullName_Ar", u."Scholar_ID"
HAVING COUNT(a."PaperID") = 0
ORDER BY u."UserID";
```

### الـ Flexible Filters
```cmd
:: Dry-run (list فقط، ما يسحب)
python rescrape_missing.py --dry-run

:: من UID معيّن لما بعدها (للـ parallel windows)
python rescrape_missing.py --start-from 13

:: UIDs محددة فقط
python rescrape_missing.py --only 8 10 13 --sleep 5

:: استثناء بعض UIDs
python rescrape_missing.py --exclude 6 8 --sleep 4
```

### Sequential vs Parallel Trade-off

| Mode | Speed | Risk |
|---|---|---|
| Sequential (`--sleep 3`) | 30 min for 37 | Low — SerpAPI accepts |
| 2 windows parallel | 15 min | Medium — possible 429 |
| 4 windows parallel (الأول) | 8 min | High — many failed |

**Lesson**: للـ SerpAPI، sequential مع gap كافي > parallel مع risk.

### النتائج
كل الـ 37 باحث تم سحبهم بنجاح في الـ second run.

---

# Part IV — Journal Classification

## 8. الـ Classification Problem (الأكبر)

### الواقع المعقّد
بعد السحب، اكتشفنا إن `Journals` table ممتلئ بأسماء قذرة:

```
jid=4    "Sensors 22 (20), 7722, 2022"   ISSN=14248220
jid=1945 "Sensors 22 (3), 1211, 2022"    ISSN=None  ← duplicate!
jid=622  "IAENG Int. J. Applied Math. 56 (1), 2026"  ISSN=None
jid=1   "Bioengineering 9 (8), 368, 2022"  ISSN=23065354
```

كل ورقة جديدة من Scholar كانت تخلق `Journals` row جديد بالـ pub string كاملاً (مع vol/issue/year).

### Impact على الـ Q-Classification
الـ dashboard query:
```sql
SELECT rp.JournalID → Journals.JournalID → JournalRankings.JournalID → Quartile
```

لو `Journals.JournalName = "Sensors 22 (5), 1234, 2024"`، الـ matching مع Scimago's `"Sensors"` يفشل.

النتيجة: 70%+ من الأوراق بدون Q-classification رغم إنها فعلاً منشورة في Q1 journals.

---

## 9. NormalizedName Migration

### `add_normalized_journal_names.py`

#### القاعدة الـ Logical
ننشئ column `NormalizedName` يحتوي canonical form قابلة للمطابقة:

```python
def normalize_journal_name(name):
    """
    'Sensors (Switzerland)'        → 'sensors'
    'IEEE Trans. Image Process.'   → 'ieee transactions image processing'
    'The Lancet'                   → 'lancet'
    'IAENG Int. J. Math. 56 (1)'   → 'iaeng international journal mathematics'
    """
    if not name: return None

    # 0. Strip vol/issue/year noise FIRST (Scholar pub strings)
    name = clean_publication_string(name)
    n = name.lower().strip()

    # 1. Drop parentheticals: "(Switzerland)", "(Basel)"
    n = re.sub(r'\s*\([^)]*\)\s*', ' ', n)

    # 2. Drop bracketed: "[Series A]"
    n = re.sub(r'\s*\[[^\]]*\]\s*', ' ', n)

    # 3. Expand abbreviations BEFORE stripping punctuation
    for pat, repl in ABBREV_MAP:
        n = re.sub(pat, repl, n)
    # Examples:
    #   'int' → 'international'
    #   'j' → 'journal'
    #   'trans' → 'transactions'
    #   'comput' → 'computer'

    # 4. Strip punctuation
    n = re.sub(r"[^a-z0-9\s]", ' ', n)

    # 5. Drop common stopwords (handles "Journal of", "The Lancet")
    drop = {'the', 'of', 'and', 'in', 'on', 'for', 'an', 'a'}
    tokens = [t for t in n.split() if t not in drop]
    n = ' '.join(tokens).strip()

    # 6. Minimum length (3+ chars — allows HLA, BMJ, AI)
    return n if len(n) >= 3 else None
```

#### الـ `clean_publication_string` Helper
```python
def clean_publication_string(pub):
    """Strip Scholar's trailing volume/issue/page/year noise.
    'IAENG Int. J. Applied Math. 56 (1), 2026' → 'IAENG Int. J. Applied Math.'
    'Sensors 23 (5), 1234, 2023' → 'Sensors'
    """
    if not pub: return pub
    # Strip from first standalone digit onwards
    cleaned = re.sub(r'\s+\d.*$', '', pub).strip().rstrip(',').strip()
    return cleaned or pub
```

#### Edge Case: Short Names
الحد الأدنى 3 chars بدل 5 (الأصلي). السبب:
- "HLA" (3 chars) — مجلة immunology معترف بها
- "BMJ" (3 chars) — British Medical Journal
- "AI" (2 chars) — لازم يبقى مرفوض (false positives على AI conferences)

#### Critical Failure Mode (وقعنا فيه)
أسماء قصيرة بعد stopword removal تتجمّع:
- `"Proceedings of the IEEE Conference on..."` بعد cleaning للـ year → `"Proceedings of the IEEE Conference on"` → بعد stopwords → `"proceedings ieee conference"` ✓ صحيح
- `"Proceedings"` لوحدها (truncated input) → `"proceedings"` ⚠ false collision مع 28 conference ثاني!

**Fix**: dataset كان فيه 28 entry اسمها فقط "Proceedings" (truncated في Scholar). consolidate_journals.py يدمجها في واحد، لكن الفعلي هي conferences مختلفة. **Trade-off accepted** لأنها rows فاضية (0 papers).

#### Force Mode
لما نغيّر الـ normalize logic، نحتاج reset + recompute:
```cmd
python add_normalized_journal_names.py --force
```

---

## 10. Improve Matching: `improve_matching.py`

### الـ 4-Pass Matching

```python
def find_journal_id_flex(cur, journal_name):
    norm = normalize_journal_name(journal_name)

    # Pass 1: exact name in Journals (LOWER match)
    cur.execute('SELECT "JournalID" FROM "Journals" WHERE LOWER("JournalName") = LOWER(%s)',
                (journal_name,))
    if r := cur.fetchone(): return r[0], 'exact_name_journals'

    # Pass 2: NormalizedName in Journals
    if norm:
        cur.execute('SELECT "JournalID" FROM "Journals" WHERE "NormalizedName" = %s', (norm,))
        if r := cur.fetchone(): return r[0], 'norm_journals'

    # Pass 3a: NormalizedName in JournalRankings → JournalID
    if norm:
        cur.execute('SELECT "JournalID" FROM "JournalRankings" '
                    'WHERE "NormalizedName" = %s AND "JournalID" IS NOT NULL', (norm,))
        if r := cur.fetchone(): return r[0], 'norm_jrankings_via_jid'

        # Pass 3b: NormalizedName → ISSN → Journals.ISSN_Print
        cur.execute('SELECT "Issn" FROM "JournalRankings" WHERE "NormalizedName" = %s', (norm,))
        if r := cur.fetchone():
            issn_norm = normalize_issn(r[0])
            cur.execute('SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s', (issn_norm,))
            if r2 := cur.fetchone(): return r2[0], 'norm_jrankings_via_issn'

    return None, None
```

### النتائج
على 2,668 paper بـ NULL JournalID:
- **1,742 matched** (65%)
- 926 unmatched (conferences, predatory, regional)

التوزيع:
- `exact_name_journals`: 829
- `norm_journals`: 913

---

## 11. Auto-Classify: الـ Comprehensive Pipeline

### `auto_classify.py` — الـ Magic Script

3 phases في pass واحد:

#### Phase 1: Auto-create Journals
```python
for paper in papers_with_null_journal_id:
    norm = normalize_journal_name(paper.publication_string)
    issn = lookup_issn_in_jrankings(norm)
    if not issn:
        continue  # not in Scimago
    
    journal_id = find_or_create_journal(issn, norm)
    update_paper(paper, journal_id)
```

`find_or_create_journal` logic:
1. ابحث في Journals بالـ ISSN → لو موجود، استخدمه
2. ابحث بالـ NormalizedName → لو موجود، backfill ISSN عليه
3. لو لا → **اخلق Journals row جديد** بـ:
   - `JournalName` = title-case من normalized
   - `ISSN_Print` = من Scimago
   - `NormalizedName` = نفس norm

#### Phase 2: Backfill ISSN
```sql
WITH all_candidates AS (
    SELECT j."JournalID", j."NormalizedName",
           (SELECT jr."Issn" FROM "JournalRankings" jr
             WHERE jr."NormalizedName" = j."NormalizedName" LIMIT 1) AS issn_pick,
           (SELECT COUNT(DISTINCT jr."Issn") FROM "JournalRankings" jr
             WHERE jr."NormalizedName" = j."NormalizedName") AS distinct_issns
    FROM "Journals" j
    WHERE j."ISSN_Print" IS NULL
),
deduped AS (
    SELECT DISTINCT ON (issn_pick) "JournalID", issn_pick
    FROM all_candidates
    WHERE distinct_issns = 1
    ORDER BY issn_pick, "JournalID"
)
UPDATE "Journals" j SET "ISSN_Print" = d.issn_pick
FROM deduped d
WHERE j."JournalID" = d."JournalID"
  AND NOT EXISTS (
      SELECT 1 FROM "Journals" j2
      WHERE j2."ISSN_Print" = d.issn_pick AND j2."JournalID" <> d."JournalID"
  )
```

`DISTINCT ON (issn_pick)` يضمن row واحد فقط لكل ISSN (يحترم الـ `unique_issn` constraint).

#### Phase 3: Link JournalRankings.JournalID
```sql
UPDATE "JournalRankings" jr
SET "JournalID" = j."JournalID"
FROM "Journals" j
WHERE regexp_replace(UPPER(jr."Issn"), '[^A-Z0-9]', '', 'g')
    = regexp_replace(UPPER(j."ISSN_Print"), '[^A-Z0-9]', '', 'g')
  AND (jr."JournalID" IS NULL OR jr."JournalID" <> j."JournalID")
  AND NOT EXISTS (
      SELECT 1 FROM "JournalRankings" jr2
      WHERE jr2."JournalID" = j."JournalID"
        AND jr2."RankingYear" = jr."RankingYear"
        AND jr2."RankingID" <> jr."RankingID"
  )
```

`NOT EXISTS` guard يحترم الـ `unique_journal_year` constraint.

### الـ Results
بعد كل الـ phases:
- 2,290 papers بـ JournalID (كان 2,139)
- 1,153 papers بـ Quartile (كان 1,002)
- 61 Journals جديدة (BioScience, HLA, إلخ)

---

## 12. Scimago Full Import

### المشكلة الجذرية
بعد الـ NormalizedName migration، اكتشفنا إن JournalRankings فيه 32k entry فقط (subset قديم). كثير journals معروفة (Cureus, BioScience, Computational Intelligence) كانت **مفقودة**.

### الـ Discovery Query
```sql
SELECT * FROM "JournalRankings" WHERE "Issn" = '21688184';  -- Cureus
-- 0 rows
```

Cureus في Scimago Q2، لكن مش في DB.

### `import_scimago_fast.py` — Bulk Strategy

#### Naive Approach (slow)
```python
for row in csv:
    cur.execute('SELECT * FROM JournalRankings WHERE Issn = %s', [issn])
    if exists:
        cur.execute('UPDATE ...')
    else:
        cur.execute('INSERT ...')
```
35k rows × 2 round trips × 100ms = **~2 hours على Neon**.

#### Fast Approach (50-100x faster)
```python
# 1. Bulk-load CSV → temp staging table
execute_values(cur, '''
    INSERT INTO _scimago_stage (issn, quartile, impact_factor, ...)
    VALUES %s
''', rows_to_load, page_size=2000)

# 2. Single UPSERT (UPDATE existing)
cur.execute('''
    UPDATE "JournalRankings" jr
    SET "Quartile" = COALESCE(s.quartile, jr."Quartile"),
        "ImpactFactor" = COALESCE(s.impact_factor, jr."ImpactFactor"),
        "NormalizedName" = COALESCE(s.normalized, jr."NormalizedName"),
        ...
    FROM _scimago_stage s
    WHERE jr."Issn" = s.issn
''')

# 3. INSERT new (ISSNs not yet in JR)
cur.execute('''
    INSERT INTO "JournalRankings" (...)
    SELECT ... FROM _scimago_stage s
    LEFT JOIN "JournalRankings" jr ON jr."Issn" = s.issn
    WHERE jr."RankingID" IS NULL
''')
```
**~30-60 ثانية** للـ 53k rows.

### Critical CSV Parsing
الـ Scimago CSV:
- Semicolon-delimited (European format)
- UTF-8 with BOM
- SJR uses comma decimal: `'0,425'` → `0.425`
- ISSN field فيه multiple ISSNs separated by `, `: `'00280836, 14764687'`

```python
def parse_sjr(s):
    return float(s.replace(',', '.')) if s else None

def parse_issns(issn_field):
    parts = issn_field.split(',')
    return [normalize_issn(p.strip()) for p in parts if p.strip()]
```

### النتائج
- 33,557 updated + 19,846 inserted = **53,403 total**
- Cureus, BioScience, HLA الحين موجودين ✓

### Lesson: Some Journals NOT in Scimago
- Cureus: delisted from Scimago 2025 (predatory journal flag)
- بعض Hindawi journals: removed
- Regional/local journals: never in

**Architectural decision**: نقبل هذا — Scimago decisions تعكس academic community trust.

---

## 13. Backfill ISSN: `backfill_journal_issn.py`

### الـ Use Case
بعد الـ Scimago import، عندنا:
- Journals: 1,776 row (709 بدون ISSN)
- JournalRankings: 53k rows (mostly with ISSN)

نحتاج نربطهم.

### الـ Logic
```sql
WITH all_candidates AS (
    SELECT j."JournalID", j."NormalizedName",
           (SELECT COUNT(DISTINCT jr."Issn") FROM "JournalRankings" jr
             WHERE jr."NormalizedName" = j."NormalizedName") AS distinct_issns,
           (SELECT jr."Issn" FROM "JournalRankings" jr
             WHERE jr."NormalizedName" = j."NormalizedName"
             LIMIT 1) AS issn_pick
    FROM "Journals" j
    WHERE j."ISSN_Print" IS NULL
)
```

نقبل الـ match فقط لو:
- `distinct_issns = 1` (unambiguous match)
- لا يوجد row ثاني عنده الـ ISSN (يحترم الـ unique constraint)

### الـ Ambiguous Cases
الـ output:
```
⚠ Ambiguous (multiple ISSNs for same name) — skipped, need manual review:
    jid=675  norm='development'  candidates=4
    jid=470  norm='international arab journal information technology'  candidates=2
    ...
```

هذي تحتاج manual review (نفس normalized name لجnals مختلفة).

---

## 14. Link Rankings: `link_rankings_to_journals.py`

### الـ Use Case
JournalRankings.JournalID FK ضعيف بعد الـ Scimago import (52k rows بدون JournalID).

### Logic
```sql
UPDATE "JournalRankings" jr
SET "JournalID" = j."JournalID"
FROM "Journals" j
WHERE j."ISSN_Print" IS NOT NULL
  AND regexp_replace(UPPER(jr."Issn"), '[^A-Z0-9]', '', 'g')
      = regexp_replace(UPPER(j."ISSN_Print"), '[^A-Z0-9]', '', 'g')
  AND (jr."JournalID" IS NULL OR jr."JournalID" <> j."JournalID")
  AND NOT EXISTS (...)  -- guard against unique_journal_year
```

`regexp_replace` يطابق ISSNs بصيغ مختلفة:
- `'21693536'` (بدون hyphen)
- `'2169-3536'` (مع hyphen)

---

# Part V — Citations

## 15. الـ Citation Architecture

### الـ Layers
```
ResearchPaper.RawData_Log->'cited_by'->>'value'  ← cumulative count من Scholar
ResearchPaper.CitationsByYear (JSONB)            ← per-year breakdown (OpenAlex)
Researcher.CitationsByYear (JSONB)               ← author-level per-year (Scholar)
```

### المشكلة
الـ dashboard كان يحسب citations من `ResearchPaper.CitationsByYear`، لكن **104 paper فقط من 3,078 عندهم بيانات** (3.4%).

النتيجة: 818 citation عرض للـ 2025+2026 بدل ~16,000 الحقيقية.

### الـ Strategic Decision
بدل ما نسوي per-paper backfill (مكلف بالـ APIs):
1. **Dashboard** يستخدم `Researcher.CitationsByYear` (Scholar's author graph)
2. **Profile chart** يستخدم نفس الـ source
3. **Per-paper modal**: يعرض `ResearchPaper.CitationsByYear` لو موجود، وإلا fallback message

### Trade-off Analysis

| Approach | Cost | Accuracy | Coverage |
|---|---|---|---|
| Per-paper من OpenAlex DOI | Free | High | 70% (DOI-only) |
| Per-paper من OpenAlex title-search | Free | Medium | 85% |
| Per-paper من SerpAPI google_scholar_cites | 1 credit/paper | High | 100% |
| **Per-researcher (Scholar graph)** | **1 credit/researcher** | **High** | **100% (researchers بـ Scholar profile)** |

اخترنا **per-researcher** لأنها:
- 100x أرخص
- نفس الـ accuracy
- Scholar's authoritative source

### Co-authorship Trade-off
لو 2 من باحثينا co-authored ورقة:
- Per-paper sum: counts ورقة مرة واحدة (correct)
- Per-researcher sum: counts الـ citations لكل واحد (slight over-count)

**Accepted** لأن الـ over-count صغير + Scholar's granularity.

---

## 16. Backfill Scripts (تطوّر متدرج)

### `backfill_citations_by_year.py` (الأول)
- DOI-only lookup
- 454 paper تم تعبئتهم
- Found 30 mins

### `backfill_citations_full.py`
- DOI lookup + title-search fallback
- Lastname validation للـ title path
- لكن تأثر بالـ rate limits

### `backfill_citations_parallel.py`
- ThreadPoolExecutor (5-10x faster)
- Configurable workers
- Backoff strategy: 10s → 30s → 60s → 120s → 180s
- Respects `Retry-After` header

### `backfill_citations_serpapi.py` (الـ premium fallback)
- 1 SerpAPI credit per paper
- Min cites threshold (`--min-cites 5`)
- Max credits cap (`--max-credits 200`)
- Confirmation prompt
- Year extraction من `publication_info.summary`

### `backfill_researcher_cby.py` (الـ Final solution) ⭐
- 1 SerpAPI credit per researcher
- Saves Scholar's `cited_by.graph` to `Researcher.CitationsByYear`
- Idempotent (skips populated)
- `--force` لـ refresh الكل

### مثال
```cmd
$ python backfill_researcher_cby.py --dry-run
Researchers needing CitationsByYear backfill: 2
SerpAPI credits to spend: 2
  UID= 11  Scholar=Pl5DTIUAAAAJ  أبو بكر يوسف ابو زيد المهدي
  UID= 68  Scholar=xaxWidcAAAAJ  عبدالله محمد عبدالهادي خطاب
[dry-run] would update 2 researchers.
```

---

## 17. Dashboard Citation Query Update

### Before (per-paper)
```sql
year_keys_expr = ' + '.join([
    f"COALESCE((rp.\"CitationsByYear\"->>%s)::int, 0)"
    for _ in years
])
SELECT COALESCE(SUM(year_keys_expr), 0)
FROM "ResearchPaper" rp
WHERE EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
```

### After (per-researcher)
```sql
year_keys_expr = ' + '.join([
    f"COALESCE((r.\"CitationsByYear\"->>%s)::int, 0)"
    for _ in years
])
SELECT COALESCE(SUM(year_keys_expr), 0)
FROM "Researcher" r
WHERE r."CitationsByYear" IS NOT NULL
```

### النتائج
- 2025: 661 → **12,587** (+1,805%)
- 2026: 157 → **3,831** (+2,340%)

---

# Part VI — UI Improvements

## 18. Q1/Q2/Q3/Q4 Filter Pills

### المشكلة
المستخدم يريد فلترة أوراق الباحث بالـ quartile.

### Design
Multi-select pills next to existing Year/Citations sort:

```
[ Q1 ][ Q2 ][ Q3 ][ Q4 ]    [ Year ↓ ][ Citations ]
```

- Empty Set = show all (no filter)
- Click pill = toggle
- Multiple pills = OR (Q1 OR Q2)

### Implementation (Angular Signals)
```typescript
readonly activeQuartiles = signal<Set<string>>(new Set());

toggleQuartile(q: string) {
  this.activeQuartiles.update(s => {
    const next = new Set(s);
    if (next.has(q)) next.delete(q);
    else next.add(q);
    return next;
  });
  this.visibleCount.set(this.LOAD_BATCH);
}

readonly sortedPapers = computed(() => {
  let all = [...(this.data()?.papers ?? [])];
  
  const quartiles = this.activeQuartiles();
  if (quartiles.size > 0) {
    all = all.filter(p => p.quartile && quartiles.has(p.quartile));
  }
  
  // ... existing sort logic
  return all;
});
```

### HTML
```html
<div class="flex items-center gap-1 bg-ink-100 rounded-full p-1">
  @for (q of ['Q1', 'Q2', 'Q3', 'Q4']; track q) {
    <button (click)="toggleQuartile(q)"
            class="text-xs font-medium px-3 py-1.5 rounded-full transition-colors"
            [class.bg-white]="activeQuartiles().has(q)"
            [class.shadow-card]="activeQuartiles().has(q)"
            [class.text-ink-700]="activeQuartiles().has(q)"
            [class.text-ink-400]="!activeQuartiles().has(q)">
      {{ q }}
    </button>
  }
</div>
```

---

## 19. Per-Year Citations Modal

### قبل
الـ paper detail modal يعرض cumulative citations فقط. لو عنده per-year data، الـ chart يظهر، وإلا يختفي.

### بعد
- يعرض الـ chart دائماً (لو فيه data)
- Below الـ chart: per-year text breakdown (`2020: 5  2021: 45  2022: 73`)
- Fallback message لو data ناقصة:
  > "Yearly breakdown not available for this paper. Total citations: 27 (per-year data is missing — typically because this paper has no DOI)"

### Code
```html
@if (chart(); as c) {
  <svg viewBox="...">...</svg>
  <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-500">
    @for (b of c.bars; track b.year) {
      <span class="font-mono">
        <span class="text-ink-400">{{ b.year }}</span>
        <span class="text-ink-700 font-medium ml-1">{{ b.count }}</span>
      </span>
    }
  </div>
} @else {
  <div class="text-xs text-ink-400 italic py-3 px-4 bg-ink-50 rounded-lg">
    Yearly breakdown not available for this paper.
    @if (p.total_citations > 0) {
      <span class="block mt-1">
        Total citations: <span class="font-medium text-ink-700">{{ fmt(p.total_citations) }}</span>
        <span class="text-ink-400">
          (per-year data is missing — typically because this paper has no DOI)
        </span>
      </span>
    }
  </div>
}
```

---

## 20. Department Research Statistics

### الـ Backend Update

#### Two queries — Papers + Citations
```sql
-- Papers per dept per year
SELECT w."DepartmentID", rp."PubYear",
       COUNT(DISTINCT rp."PaperID") AS papers
FROM "Works_In" w
JOIN "Authors" a ON a."UserID" = w."UserID"
JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
WHERE w."IsCurrentPosition" = TRUE
  AND rp."PubYear" = ANY(%s)
GROUP BY w."DepartmentID", rp."PubYear"
```

```sql
-- Citations per dept per year (uses Researcher.CitationsByYear)
SELECT w."DepartmentID",
       year_kv.key::int AS year,
       SUM((year_kv.value)::int) AS citations
FROM "Works_In" w
JOIN "Researcher" r ON r."UserID" = w."UserID"
CROSS JOIN LATERAL jsonb_each_text(
    COALESCE(r."CitationsByYear", '{}'::jsonb)
) AS year_kv
WHERE w."IsCurrentPosition" = TRUE
  AND year_kv.value ~ '^[0-9]+$'
  AND year_kv.key::int = ANY(%s)
GROUP BY w."DepartmentID", year_kv.key::int
```

#### Inject into response
```python
for d in departments:
    did = d['department_id']
    d['by_year'] = [
        {
            'year': y,
            'papers':    papers_by_dept_year.get(did, {}).get(y, 0),
            'citations': cites_by_dept_year.get(did, {}).get(y, 0),
        }
        for y in sorted(years)
    ]
```

### الـ Frontend Card Layout
```html
<section class="bg-white rounded-apple shadow-card p-6 mb-8">
  <h2 class="text-lg font-semibold text-ink-700 mb-6">
    Department Research Statistics
  </h2>
  <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
    @for (dept of d.departments; track dept.department_id) {
      <div class="border border-ink-200 rounded-lg p-5">
        <div class="flex items-baseline justify-between mb-3" dir="rtl">
          <h3 class="text-base font-semibold text-ink-900">
            {{ dept.department_name }}
          </h3>
          <div class="text-xs text-ink-400" dir="ltr">
            {{ dept.total_researchers }} Total ·
            {{ dept.active_researchers }} Active
          </div>
        </div>
        <table class="w-full text-sm">
          <thead>...</thead>
          <tbody>
            @for (yr of dept.by_year; track yr.year) {
              <tr>
                <td>{{ yr.year }}</td>
                <td class="text-right">{{ fmt(yr.papers) }}</td>
                <td class="text-right">{{ fmt(yr.citations) }}</td>
              </tr>
            }
            <tr class="font-semibold">
              <td>Total</td>
              <td class="text-right">{{ fmt(deptTotalPapers(dept)) }}</td>
              <td class="text-right">{{ fmt(deptTotalCitations(dept)) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    }
  </div>
</section>
```

### Section Reordering
الترتيب الجديد في الـ overview dashboard:
1. KPI cards (Researchers/Publications/Citations/h-index)
2. **Department Research Statistics** (full-width)
3. **Top Researchers** (full-width)
4. Yearly Department Breakdown (existing collapsible)

### Excel Export Sync
الـ Excel sheets الحين تستخدم نفس الـ source:
- `Summary {year}` — Citations من `Researcher.CitationsByYear`
- `Departments {year}` — citations subquery via Researcher

---

# Part VII — Database Operations

## 21. الـ Schema Constraints

### Discovered During Migrations

#### `unique_issn` على Journals.ISSN_Print
**Implication**: Multiple Journals بنفس ISSN ممنوع.

**في الـ scripts**: 
```python
# قبل update
cur.execute('SELECT 1 FROM Journals WHERE ISSN_Print = %s AND JournalID <> %s LIMIT 1',
            (issn, this_jid))
if not cur.fetchone():
    cur.execute('UPDATE Journals SET ISSN_Print = %s WHERE JournalID = %s',
                (issn, this_jid))
```

أو استخدم `DISTINCT ON` في الـ batch updates:
```sql
WITH deduped AS (
    SELECT DISTINCT ON (issn_pick) "JournalID", issn_pick
    FROM candidates
    ORDER BY issn_pick, "JournalID"
)
UPDATE ...
```

#### `unique_journal_year` على JournalRankings (JournalID, RankingYear)
**Implication**: ranking واحد لكل journal لكل year.

```sql
UPDATE "JournalRankings" jr SET "JournalID" = j."JournalID"
FROM "Journals" j
WHERE ...
  AND NOT EXISTS (
      SELECT 1 FROM "JournalRankings" jr2
      WHERE jr2."JournalID" = j."JournalID"
        AND jr2."RankingYear" = jr."RankingYear"
        AND jr2."RankingID" <> jr."RankingID"
  )
```

#### Title UNIQUE على ResearchPaper
**Implication**: عناوين متطابقة ممنوعة.

```python
try:
    cur.execute('SAVEPOINT sp')
    cur.execute('INSERT INTO "ResearchPaper" (...) RETURNING "PaperID"', ...)
    paper_id = cur.fetchone()[0]
    cur.execute('RELEASE SAVEPOINT sp')
except psycopg2.errors.UniqueViolation:
    cur.execute('ROLLBACK TO SAVEPOINT sp')
    cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("Title") = LOWER(%s)',
                (title,))
    paper_id = cur.fetchone()[0]
```

---

## 22. الـ Connection Strategy

### `.env` Configuration
```bash
# Local DB (fallback)
DB_NAME=LitrixDB
DB_USER=postgres
DB_PASSWORD=<your-local-password>
DB_HOST=localhost
DB_PORT=5432

# Neon (production)
# Full connection string lives in .env (never commit it).
# Format: postgresql://<USER>:<PASSWORD>@<HOST>/<DB>?sslmode=require
DATABASE_URL=<see .env — never commit the live string>
```

> **Security note:** never paste live connection strings or credentials
> into this file (or any committed doc). The single source of truth is
> `.env`, which is gitignored. Use placeholders here.

### Universal `db()` Function (في كل scripts)
```python
def db():
    keepalive_kwargs = dict(
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url, **keepalive_kwargs)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "LitrixDB"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        **keepalive_kwargs,
    )
```

### Critical Issue: Neon Idle Timeout
Neon يقفل connections بعد ~5 min idle. الحل:

#### Layer 1: TCP Keepalives
```python
keepalive_kwargs = dict(
    keepalives=1,           # enable
    keepalives_idle=30,     # send probe بعد 30s idle
    keepalives_interval=10, # repeat كل 10s
    keepalives_count=5,     # 5 failures before connection drop
)
```

#### Layer 2: Auto-Reconnect Loop
```python
def safe_execute(conn, cur, query, params, retries=3):
    for attempt in range(retries):
        try:
            cur.execute(query, params)
            return cur
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            print(f"⚠ DB connection lost: {e}")
            try: conn.close()
            except: pass
            conn = db()
            cur = conn.cursor()
            time.sleep(2)
    raise
```

#### Layer 3: Resume-Friendly
```python
# Re-query work to do — skips already-done items
cur.execute('SELECT ... WHERE flag IS NULL')
```

---

## 23. الـ DB Migrations Done

### Schema Changes
```sql
-- 1. Journals: NormalizedName + index
ALTER TABLE "Journals" ADD COLUMN "NormalizedName" TEXT;
CREATE INDEX idx_journals_normname ON "Journals"("NormalizedName");

-- 2. JournalRankings: NormalizedName + index
ALTER TABLE "JournalRankings" ADD COLUMN "NormalizedName" TEXT;
CREATE INDEX idx_jrankings_normname ON "JournalRankings"("NormalizedName");
```

### Data Migrations
- Backfill `Journals.NormalizedName` (1,837 rows)
- Backfill `JournalRankings.NormalizedName` (53,403 rows)
- Backfill `Journals.ISSN_Print` from JR (~10 rows updated)
- Backfill `Researcher.CitationsByYear` (2 rows updated, 65 already had data)
- Backfill `ResearchPaper.CitationsByYear` (~454 rows from OpenAlex DOI lookup)
- Re-link `JournalRankings.JournalID` (~750+ rows)

---

# Part VIII — الخلاصة العملية

## 24. Pipeline لباحث جديد

```cmd
:: Step 1: السحب — اختر Source
:: Option A: Scholar
python sync_researcher.py <scholar_id> <user_id>

:: Option B: ORCID
python sync_by_orcid.py --orcid <ORCID> --user <user_id>

:: Option C: Manual (BibTeX-style، edit RESEARCHERS dict أولاً)
python add_profile_papers.py --uid <user_id> --manual

:: Step 2: التصنيف الشامل
python auto_classify.py

:: Step 3: Per-year graph (لو الـ sync ما عبّاها)
python backfill_researcher_cby.py
```

---

## 25. Pipeline لـ Bulk Operations

### Refresh كل المجلات
```cmd
:: 1. Re-import Scimago (لو عندك CSV جديد)
python import_scimago_fast.py "scimagojr2026.csv"

:: 2. Re-normalize (لو غيّرت normalize logic)
python add_normalized_journal_names.py --force

:: 3. Re-classify (يتأكد كل شي مربوط)
python auto_classify.py
```

### Refresh كل الباحثين
```cmd
:: 1. Re-sync from Scholar (للي عندهم Scholar_ID)
python rescrape_missing.py  # لو في 0-papers researchers
:: أو re-sync مفصّل:
python sync_researcher.py <scholar_id> <user_id>  # واحد واحد

:: 2. Re-fetch citation graphs
python backfill_researcher_cby.py --force
```

---

## 26. الـ Files Map

### Production Scripts (المهمة)
```
litrix/
├── sync_researcher.py              ← Main scraper (Scholar + OpenAlex)
├── sync_by_orcid.py                ← ORCID-based sync
├── add_profile_papers.py           ← Manual entry (5 researchers)
├── add_alomari_papers.py           ← Alomari's 4 BibTeX papers
├── rescrape_missing.py             ← Rescrape researchers with 0 papers
├── auto_classify.py                ← ⭐ Comprehensive journal classification
├── add_normalized_journal_names.py ← NormalizedName migration
├── improve_matching.py             ← Match papers to journals
├── backfill_journal_issn.py        ← Backfill ISSN from JournalRankings
├── link_rankings_to_journals.py    ← Link JR.JournalID via ISSN
├── consolidate_journals.py         ← Merge duplicate Journal rows
├── import_scimago_fast.py          ← Bulk import Scimago CSV
├── backfill_researcher_cby.py      ← ⭐ Researcher-level Citations
├── backfill_citations_by_year.py   ← Per-paper from OpenAlex DOI
├── backfill_citations_parallel.py  ← Parallel per-paper
├── backfill_citations_serpapi.py   ← Per-paper from SerpAPI
└── verify_all_researchers.py       ← Cleanup wrong attributions
```

### Diagnostic Scripts
```
├── check_columns.py            ← Schema inspection
├── check_sensors.py            ← Specific journal lookup
├── check_cby_status.py         ← CitationsByYear coverage
├── check_dashboard_sum.py      ← Verify dashboard query
├── check_jr_year.py            ← Scimago import year/source
├── check_almashlah_cby.py      ← Specific researcher data
├── check_cureus_jr.py          ← Specific journal in Scimago
├── check_citations.py          ← Citations storage analysis
├── check_citations_by_year.py  ← Per-year data status
├── verify_cureus.py            ← Multi-angle journal search
└── diagnose_unranked.py        ← Top unranked journals
```

### Backend
```
backend/
├── analytics/
│   ├── views.py        ← API endpoints (overview, profile, departments)
│   ├── models.py       ← (existing — Users, Researcher, ResearchPaper, etc.)
│   └── serializers.py  ← (existing)
└── litrix_backend/
    └── settings.py
```

### Frontend
```
frontend/src/app/components/
├── overview-dashboard/
│   ├── overview-dashboard.component.ts
│   └── overview-dashboard.component.html  ← KPIs + Departments + Top Researchers
├── researcher-profile/
│   ├── researcher-profile.component.ts    ← Q-filters logic
│   └── researcher-profile.component.html  ← Sort + Q1-Q4 pills
└── paper-detail-modal/
    ├── paper-detail-modal.component.ts
    └── paper-detail-modal.component.html  ← Per-year breakdown
```

---

## 27. الـ Architectural Principles (Reminders)

> **1. Source of Truth Hierarchy**:
> Scholar's articles[] = canonical attribution.
> OpenAlex/CrossRef/SerpAPI/ORCID = enrichment only.
> No fuzzy author name matching.

> **2. NormalizedName Pattern**:
> Strip vol/issue/year noise.
> Expand abbreviations.
> Drop stopwords.
> Allow short names (3+ chars).

> **3. Researcher.CitationsByYear للـ Dashboard**:
> Per-paper backfill expensive و non-essential.
> Scholar's author-level graph = authoritative.

> **4. DATABASE_URL في .env = Single Switch**:
> Local fallback إذا empty.
> TCP keepalives ضد Neon idle timeout.
> Auto-reconnect على connection drops.

> **5. Idempotent Operations**:
> All scripts safe to re-run.
> SAVEPOINT + ROLLBACK للـ unique constraint handling.
> COALESCE بدل overwrites.

---

## 28. الـ Lessons Learned

1. **Always validate by deterministic ID**, ما تعتمد على names فقط
2. **Normalize aggressively** — الـ Scholar pub strings filthy
3. **Schema constraints** يكشفون نفسهم في أوقات حرجة — اختبر الـ batches قبل الـ full runs
4. **Rate limits exist for a reason** — backoff، parallel limits، and monitoring matter
5. **Trade-offs are real** — accuracy vs speed vs cost. اختار اللي يناسب الـ use case
6. **Debug scripts قيّمة** — `check_*.py` ساعدنا نشخّص بسرعة
7. **Documentation is code** — هذا الـ doc منع تكرار الأخطاء

---

## 29. الـ Future Work

### Pending
- [ ] Per-paper CitationsByYear backfill via SerpAPI (708 papers لـ Almashlah)
- [ ] Consolidate duplicate Journals rows (cosmetic)
- [ ] Dashboard year filter (2024 + 2025 + 2026)
- [ ] Department-specific researcher profiles

### Nice-to-Have
- [ ] Title column in JournalRankings (لـ richer matching)
- [ ] CiteScore/JCR rankings كـ alternative sources
- [ ] Trend analysis للـ NLP/Research Chatbot

---

## 30. Acknowledgments

**Architectural Decisions Made With**:
- Stack overflow + PostgreSQL docs (للـ window functions, JSONB, regexp_replace)
- OpenAlex docs (counts_by_year structure)
- SerpAPI docs (google_scholar_author + google_scholar_cites)
- Scimago Journal Rank (CSV format + delisting policies)

**Pain Points Survived**:
- Neon SSL idle timeouts (4 hours debugging)
- Scholar's similar-name disambiguation (3 false-positive incidents)
- Rate limit cascades (16,701 second ban once 😅)
- Mixed identities on ResearchGate (ممدوح ميرغني case)

---

**Last Updated**: May 2026
**Maintainer**: Rawan
**Database**: Neon (production) + Local PostgreSQL (development)
**Frontend**: https://litrix.vercel.app
**Backend**: Render (auto-deploy from main branch)
