"""
Litrix Scraper - Patched Version (v3, schema-aligned with actual DB)
====================================================================
Aligned with the real DB schema (verified via information_schema).

3-Tier Logical Separation (single file, ready to split for Django):
    Layer 1: CONFIG     -> env, settings
    Layer 2: CLIENTS    -> external APIs (SerpAPI, CrossRef)
    Layer 3: PARSERS    -> pure business logic (no I/O)
    Layer 4: REPOSITORY -> DB operations (per entity)
    Layer 5: PIPELINE   -> orchestration

Schema notes (matching the real LitrixDB):
    - "Users" (plural) holds Scholar_ID + Litrix_ID directly.
    - "Researcher" extends Users via UserID FK.
    - "ISSN_Mapping" is a (ISSN -> RankingID) lookup.
    - "JournalRankings" is pre-populated from Scimago import,
      but JournalID is NULL until we link it here.
    - "Authors" carries AuthorNameRaw + MappingConfidence + MappingCriteria
      for full disambiguation provenance.
    - "ExternalAuthors" is paper-scoped (FullName + Affiliation + PaperID).

The 4 critical fixes from v1 are preserved:
    1. Co-authors persisted (registered -> Authors, others -> ExternalAuthors).
    2. Two-stage dedup: DOI > NormalizedTitle.
    3. Journal upsert never overwrites valid ISSNs.
    4. JournalRankings linked to the correct JournalID via ISSN_Mapping.
"""

import os
import re
import time
import random
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Tuple, List, Dict

import psycopg2
from psycopg2.extras import Json
import httpx
from serpapi import GoogleSearch
from dotenv import load_dotenv


load_dotenv()

SERP_API_KEY = os.getenv("SERP_API_KEY")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "ra20awn@gmail.com")

DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME", "LitrixDB"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     os.getenv("DB_PORT", "5432"),
}

CROSSREF_HEADERS = {
    "User-Agent": f"Litrix/1.0 (mailto:{CONTACT_EMAIL})"
}

OPENALEX_BASE_URL = "https://api.openalex.org"
OPENALEX_HEADERS = {
    "User-Agent": f"Litrix/1.0 (mailto:{CONTACT_EMAIL})"
}
OPENALEX_TIMEOUT = 30.0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)


def fetch_scholar_profile(scholar_id: str) -> Tuple[str, List[Dict]]:
    """
    Fetch full author profile and ALL articles from Google Scholar via SerpAPI.
    Paginated (100 per page). Returns (author_name, articles).
    """
    all_articles: List[Dict] = []
    author_name = "Unknown"
    start = 0

    while True:
        params = {
            "engine":    "google_scholar_author",
            "author_id": scholar_id,
            "api_key":   SERP_API_KEY,
            "start":     start,
            "num":       100,
        }
        results = GoogleSearch(params).get_dict()

        if "error" in results:
            raise RuntimeError(f"SerpAPI: {results['error']}")

        if author_name == "Unknown":
            author_name = results.get('author', {}).get('name', 'Researcher')

        articles = results.get('articles', [])
        if not articles:
            break

        all_articles.extend(articles)
        logging.info(f"Fetched {len(all_articles)} articles so far...")

        if len(articles) < 100:
            break
        start += 100
        time.sleep(random.uniform(1.0, 2.0))

    return author_name, all_articles


def fetch_crossref_metadata(title: str) -> Tuple[Optional[str], List[str], Dict]:
    """Get DOI, ISSNs and extra metadata from CrossRef."""
    extras: Dict = {}
    try:
        response = httpx.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 1},
            headers=CROSSREF_HEADERS,
            timeout=20.0,
        )
        if response.status_code == 200:
            items = response.json().get('message', {}).get('items', [])
            if items:
                item = items[0]
                doi = item.get('DOI')
                issns = [
                    s.replace('-', '').strip()
                    for s in item.get('ISSN', []) if s
                ]
                extras = {
                    "volume":   item.get('volume'),
                    "issue":    item.get('issue'),
                    "pages":    item.get('page'),
                    "abstract": item.get('abstract'),
                    "language": item.get('language'),
                }
                return doi, issns, extras
    except (httpx.HTTPError, ValueError) as e:
        logging.warning(f"CrossRef miss for '{title[:40]}': {e}")
    return None, [], extras


_ORCID_RE  = re.compile(r'^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$')
_OAID_RE   = re.compile(r'^A\d+$')


def fetch_openalex_author_works(orcid_or_oaid: str) -> Tuple[str, List[Dict]]:
    """
    Fetch ALL works for an OpenAlex author. Used by the OpenAlex-primary
    sync flow (researchers who have an ORCID but no Scholar_ID).

    Accepts either:
        - An ORCID like '0000-0001-2345-6789'
        - An OpenAlex Author ID like 'A5012345678'

    Returns (display_name, list_of_work_dicts). The list is the FULL set,
    paginated transparently using OpenAlex's cursor-based pagination.

    Why cursor pagination instead of offset? OpenAlex caps offset at 10k,
    cursor is unbounded. For prolific authors (300+ works) cursor is the
    only correct choice.
    """
    ident = (orcid_or_oaid or '').strip()
    if _ORCID_RE.match(ident):
        author_url = f"{OPENALEX_BASE_URL}/authors/orcid:{ident}"
        works_filter = f"author.orcid:{ident}"
    elif _OAID_RE.match(ident):
        author_url = f"{OPENALEX_BASE_URL}/authors/{ident}"
        works_filter = f"author.id:{ident}"
    else:
        raise ValueError(
            f"Invalid identifier '{ident}': expected ORCID or 'A<digits>'"
        )

    author_name = "Unknown"
    try:
        resp = httpx.get(
            author_url,
            headers=OPENALEX_HEADERS,
            params={"mailto": CONTACT_EMAIL},
            timeout=OPENALEX_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            author_name = resp.json().get('display_name') or 'Unknown'
        elif resp.status_code == 404:
            logging.warning(f"OpenAlex: author '{ident}' not found")
            return author_name, []
    except (httpx.HTTPError, ValueError) as e:
        logging.error(f"OpenAlex author resolve failed for '{ident}': {e}")
        return author_name, []

    all_works: List[Dict] = []
    cursor: Optional[str] = "*"
    while cursor:
        try:
            resp = httpx.get(
                f"{OPENALEX_BASE_URL}/works",
                headers=OPENALEX_HEADERS,
                params={
                    "filter":   works_filter,
                    "per-page": 100,
                    "cursor":   cursor,
                    "mailto":   CONTACT_EMAIL,
                },
                timeout=OPENALEX_TIMEOUT,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                logging.warning(
                    f"OpenAlex works pagination ended at status "
                    f"{resp.status_code} for '{ident}'"
                )
                break
            data = resp.json()
            results = data.get('results', []) or []
            if not results:
                break
            all_works.extend(results)
            logging.info(f"  Fetched {len(all_works)} works so far...")
            cursor = (data.get('meta') or {}).get('next_cursor')
            time.sleep(random.uniform(0.5, 1.0))
        except (httpx.HTTPError, ValueError) as e:
            logging.error(f"OpenAlex pagination error for '{ident}': {e}")
            break

    return author_name, all_works


def find_openalex_author_by_scopus(scopus_id: str) -> Optional[str]:
    """
    Resolve a Scopus Author ID to its canonical OpenAlex Author ID.

    OpenAlex stores Scopus IDs as full Scopus URLs in the `ids.scopus`
    field. Bare numeric IDs are rejected by the filter (HTTP 400). We
    try the documented URL formats in order; the first match wins.

    Returns the short-form OpenAlex ID (e.g., 'A5012345678') or None.
    """
    sid = (scopus_id or '').strip()
    if not sid:
        return None

    candidate_urls = [
        f"http://www.scopus.com/inward/authorDetails.url?authorID={sid}&partnerID=MN8TOARS",
        f"https://api.elsevier.com/content/author/author_id/{sid}",
    ]

    for url_form in candidate_urls:
        try:
            resp = httpx.get(
                f"{OPENALEX_BASE_URL}/authors",
                headers=OPENALEX_HEADERS,
                params={
                    "filter":   f"ids.scopus:{url_form}",
                    "per-page": 1,
                    "mailto":   CONTACT_EMAIL,
                },
                timeout=OPENALEX_TIMEOUT,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                continue
            results = (resp.json() or {}).get('results', []) or []
            if results:
                return extract_openalex_id(results[0].get('id'))
        except (httpx.HTTPError, ValueError) as e:
            logging.warning(
                f"OpenAlex Scopus lookup attempt failed for '{sid}': {e}"
            )
            continue

    logging.warning(
        f"OpenAlex doesn't have Scopus ID '{sid}' indexed under any "
        f"of the tried URL formats. Manual lookup recommended."
    )
    return None


def find_openalex_author_by_name(
    name: str,
    country_code: Optional[str] = "SA",
    institution_keywords: Optional[List[str]] = None,
    max_works_threshold: int = 200,
) -> Optional[Tuple[str, float]]:
    """
    Search OpenAlex for an author by display name, with multiple layers of
    false-positive defense.

    THE FALSE-POSITIVE PROBLEM:
        Saudi names like "Mohammed Alzahrani" or "Ahmed Alghamdi" are
        extremely common. Even filtered by country=SA, OpenAlex may return
        several distinct researchers. The naive top-result heuristic lets
        a famous prolific author "steal" the identity of an unknown one
        (we hit a case where the algo grabbed an A5* with 1400+ works
        for a junior assistant professor).

    DEFENSE LAYERS:
        1. Country filter      → narrows the candidate pool dramatically
        2. Institution keywords → optional substring match against affil.
                                 (e.g. ["Al-Baha", "Al Baha", "Albaha"])
        3. Works-count sanity  → reject candidates with works_count >
                                 max_works_threshold (default 200). A
                                 mismatched 'famous' author triggers this.
        4. Single-result rule  → only return a match if AT MOST ONE
                                 candidate survives all filters. Multiple
                                 survivors → return None (admin-review).

    Returns (openalex_id, confidence) or None. Confidence:
        - 1 unique result with all filters applied → 0.8
        - 1 unique result with country only        → 0.7
        - 0 results / multiple ambiguous results   → None  (skip!)
    """
    n = (name or '').strip()
    if not n:
        return None

    def _do_search(filter_str: Optional[str]) -> List[Dict]:
        params = {
            "search":   n,
            "per-page": 25,
            "mailto":   CONTACT_EMAIL,
        }
        if filter_str:
            params["filter"] = filter_str
        try:
            resp = httpx.get(
                f"{OPENALEX_BASE_URL}/authors",
                headers=OPENALEX_HEADERS,
                params=params,
                timeout=OPENALEX_TIMEOUT,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return []
            return (resp.json() or {}).get('results', []) or []
        except (httpx.HTTPError, ValueError) as e:
            logging.warning(f"OpenAlex name search failed for '{n}': {e}")
            return []

    def _filter_candidates(cands: List[Dict]) -> List[Dict]:
        """Apply works-count + institution filters."""
        out: List[Dict] = []
        for c in cands:
            wc = c.get('works_count') or 0
            if wc > max_works_threshold:
                logging.info(
                    f"  rejecting '{c.get('display_name')}' "
                    f"(A{extract_openalex_id(c.get('id'))[1:] if c.get('id') else '?'}) "
                    f"— works_count={wc} exceeds threshold {max_works_threshold}"
                )
                continue
            if institution_keywords:
                affil = ((c.get('last_known_institutions') or [{}])[0] or {})
                affil_name = (affil.get('display_name') or '').lower()
                if not any(kw.lower() in affil_name for kw in institution_keywords):
                    logging.info(
                        f"  rejecting '{c.get('display_name')}' "
                        f"— institution '{affil.get('display_name')}' "
                        f"doesn't match keywords {institution_keywords}"
                    )
                    continue
            out.append(c)
        return out

    pool: List[Dict] = []
    if country_code:
        pool = _do_search(
            f"last_known_institutions.country_code:{country_code}"
        )

    if not pool:
        logging.warning(
            f"  no candidates for '{n}' with country={country_code} — refusing match"
        )
        return None

    survivors = _filter_candidates(pool)

    if len(survivors) == 0:
        logging.warning(
            f"  all {len(pool)} candidates for '{n}' rejected by filters — "
            f"no safe match"
        )
        return None

    if len(survivors) > 1:
        logging.warning(
            f"  ⚠ {len(survivors)} candidates survived filters for '{n}' — "
            f"refusing ambiguous match (admin must pick manually)"
        )
        for c in survivors:
            inst = ((c.get('last_known_institutions') or [{}])[0] or {})
            logging.warning(
                f"    candidate: {c.get('display_name')} | "
                f"works={c.get('works_count')} | "
                f"affil={inst.get('display_name')}"
            )
        return None

    top = survivors[0]
    oaid = extract_openalex_id(top.get('id'))
    if not oaid:
        return None
    confidence = 0.8 if institution_keywords else 0.7
    return oaid, confidence


def _normalize_doi(doi: str) -> str:
    """
    OpenAlex accepts DOIs in multiple formats. We normalize to the bare DOI
    form (no scheme, no doi: prefix) and lowercase — DOIs are case-insensitive.
    """
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


def fetch_openalex_work_by_doi(doi: str) -> Optional[Dict]:
    """
    Fetch a single work from OpenAlex by DOI. Returns the raw JSON dict
    on success, None on 404 / network failure.

    Why DOI-keyed lookup? It's the strongest cross-source dedup signal — a
    DOI returned by Scholar's CrossRef enrichment will match the SAME work
    in OpenAlex deterministically, so merging is unambiguous.
    """
    if not doi:
        return None

    normalized = _normalize_doi(doi)
    if not normalized:
        return None

    url = f"{OPENALEX_BASE_URL}/works/doi:{normalized}"
    try:
        response = httpx.get(
            url,
            headers=OPENALEX_HEADERS,
            params={"mailto": CONTACT_EMAIL},
            timeout=OPENALEX_TIMEOUT,
            follow_redirects=True,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return None
        logging.warning(
            f"OpenAlex unexpected status {response.status_code} for DOI {normalized}"
        )
        return None
    except (httpx.HTTPError, ValueError) as e:
        logging.warning(f"OpenAlex miss for DOI '{normalized}': {e}")
        return None


CONFERENCE_KEYWORDS = ('conference', 'proceedings', 'symposium',
                      'workshop', 'meeting')


def normalize_title(title: str) -> str:
    """Aggressive normalization for paper-dedup matching."""
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def normalize_person_name(name: str) -> str:
    """Normalize an author name for disambiguation."""
    if not name:
        return ""
    n = name.strip().lower()
    n = re.sub(r'[\.,]', '', n)
    n = re.sub(r'\s+', ' ', n)
    return n


def split_full_name(full_name: str) -> Tuple[str, Optional[str], str]:
    """Split into (first, middle, last)."""
    full_name = (full_name or "").strip()
    if ',' in full_name:
        parts = full_name.split(',', 1)
        last = parts[0].strip()
        rest = parts[1].strip().split()
        first = rest[0] if rest else "Unknown"
        middle = " ".join(rest[1:]) if len(rest) > 1 else None
        return first, middle, last

    parts = full_name.split()
    if not parts:
        return "Unknown", None, "Researcher"
    if len(parts) == 1:
        return parts[0], None, "Unknown"
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def parse_coauthors(article: Dict) -> List[str]:
    """Extract co-author names from a SerpAPI article entry."""
    raw = article.get('authors')
    if not raw:
        return []
    if isinstance(raw, list):
        return [
            ((a.get('name') if isinstance(a, dict) else str(a)) or '').strip()
            for a in raw if a
        ]
    return [n.strip() for n in str(raw).split(',') if n.strip()]


def determine_venue_type(name: str) -> str:
    return 'Conference' if any(
        kw in (name or '').lower() for kw in CONFERENCE_KEYWORDS
    ) else 'Journal'


def safe_int(value) -> Optional[int]:
    try:
        if value is None:
            return None
        s = str(value).strip()
        return int(s) if s.isdigit() else None
    except (ValueError, TypeError):
        return None


def detect_language(text: str) -> str:
    """Naive Arabic/English detector for the Language column."""
    if not text:
        return 'en'
    arabic_chars = sum(1 for c in text if '؀' <= c <= 'ۿ')
    return 'ar' if arabic_chars > len(text) * 0.3 else 'en'


def reconstruct_abstract_from_inverted_index(
    inverted_index: Optional[Dict[str, List[int]]]
) -> Optional[str]:
    """
    OpenAlex returns abstracts as an *inverted index* (word → [positions])
    instead of plain text, for licensing reasons. We reconstruct the original
    flat text by sorting all (position, word) pairs and joining.

    Example input:
        {"Despite": [0], "growing": [1], "interest": [2], "in": [3, 8], ...}
    Example output:
        "Despite growing interest in ... in ..."

    Returns None if input is empty/missing — caller can fall back to other
    sources (CrossRef abstract, Scholar snippet, etc.).
    """
    if not inverted_index or not isinstance(inverted_index, dict):
        return None

    positions: List[Tuple[int, str]] = []
    for word, indices in inverted_index.items():
        if not isinstance(indices, list):
            continue
        for idx in indices:
            if isinstance(idx, int):
                positions.append((idx, word))

    if not positions:
        return None

    positions.sort(key=lambda p: p[0])
    return " ".join(word for _, word in positions)


def extract_openalex_id(full_url: Optional[str]) -> Optional[str]:
    """
    OpenAlex IDs come back as full URLs ('https://openalex.org/W2741809807').
    For storage we want the short form ('W2741809807' or 'A5012345678').
    """
    if not full_url:
        return None
    return full_url.rstrip('/').rsplit('/', 1)[-1] or None


def extract_orcid(orcid_url: Optional[str]) -> Optional[str]:
    """
    OpenAlex returns ORCID as 'https://orcid.org/0000-0001-2345-6789'.
    We store just the bare ID — easier to display, validate, and join on.
    """
    if not orcid_url:
        return None
    bare = orcid_url.rstrip('/').rsplit('/', 1)[-1]
    if re.match(r'^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$', bare):
        return bare
    return None


def parse_openalex_authorships(work: Dict) -> List[Dict]:
    """
    Extract a flat list of authors from an OpenAlex work, each enriched with
    OpenAlex ID, ORCID, and primary institution. The order is preserved
    (OpenAlex returns authorships in author-order).

    Returns: [
        {
          'name'        : 'Display Name',
          'openalex_id' : 'A5012345678' | None,
          'orcid'       : '0000-0001-2345-6789' | None,
          'affiliation' : 'King Abdulaziz University' | None,
          'position'    : 'first' | 'middle' | 'last' | None,
        },
        ...
    ]
    """
    out: List[Dict] = []
    for a in work.get('authorships', []) or []:
        author = a.get('author', {}) or {}
        institutions = a.get('institutions', []) or []
        primary_inst = institutions[0].get('display_name') if institutions else None

        out.append({
            'name':        (author.get('display_name') or '').strip(),
            'openalex_id': extract_openalex_id(author.get('id')),
            'orcid':       extract_orcid(author.get('orcid')),
            'affiliation': primary_inst,
            'position':    a.get('author_position'),
        })
    return out


def parse_openalex_journal(work: Dict) -> Tuple[Optional[str], List[str]]:
    """
    Extract (journal_name, [issns]) from OpenAlex's primary_location.source.
    OpenAlex normalizes ISSNs without dashes already, but we re-strip to
    match the format the rest of the pipeline uses.
    """
    src = (work.get('primary_location') or {}).get('source') or {}
    name = src.get('display_name')
    issns = src.get('issn') or []
    if isinstance(issns, str):
        issns = [issns]
    cleaned = [i.replace('-', '').strip() for i in issns if i]
    return name, cleaned


def parse_openalex_extras(work: Dict) -> Dict:
    """
    Build an `extras` dict in the SAME shape that fetch_crossref_metadata
    returns, so the existing `insert_paper` flow doesn't need to change
    its signature. This is the merge layer's contract.
    """
    biblio = work.get('biblio') or {}
    pages = None
    if biblio.get('first_page') and biblio.get('last_page'):
        pages = f"{biblio['first_page']}-{biblio['last_page']}"
    elif biblio.get('first_page'):
        pages = biblio['first_page']

    abstract = reconstruct_abstract_from_inverted_index(
        work.get('abstract_inverted_index')
    )

    return {
        "volume":   biblio.get('volume'),
        "issue":    biblio.get('issue'),
        "pages":    pages,
        "abstract": abstract,
        "language": work.get('language'),
    }


def extract_name_from_rg_url(rg_url: Optional[str]) -> Optional[str]:
    """
    ResearchGate URLs encode the researcher's name in the path slug:
        https://www.researchgate.net/profile/Reem-Aljoufi
        https://www.researchgate.net/profile/Mohammed-Alzahrani-51

    We extract the slug, strip trailing disambiguation digits ('-51'),
    and convert dashes to spaces. The result is plain English name we
    can feed to OpenAlex's name search.

    Returns None if the URL doesn't match the expected pattern.
    """
    if not rg_url:
        return None
    match = re.search(r'/profile/([^/?#]+)', rg_url)
    if not match:
        return None
    slug = match.group(1)
    name = slug.replace('-', ' ').replace('_', ' ')
    name = re.sub(r'\s+\d+$', '', name)
    name = re.sub(r'^(Dr|Prof|Eng)(?=\w)', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^(Dr|Prof|Eng)\.?\s+', '', name, flags=re.IGNORECASE)
    return ' '.join(name.split())


def openalex_work_to_article_shape(work: Dict) -> Dict:
    """
    Convert an OpenAlex work to the same dict shape that Scholar articles
    have, so process_article can consume both without conditional branches.

    The shape we mirror is what SerpAPI's google_scholar_author engine
    returns per article: {title, year, authors[], publication, cited_by}.

    Note: this is an *adapter*, not an enrichment. It only extracts the
    fields process_article needs to identify the paper. The full OpenAlex
    work is passed separately as `prefetched_openalex_work` so the merge
    layer has access to ORCIDs, abstracts, biblio, concepts, etc.
    """
    title = (work.get('title') or work.get('display_name') or '').strip()
    pub_year = work.get('publication_year')

    author_names: List[str] = []
    for a in (work.get('authorships') or []):
        author = a.get('author') or {}
        name = (author.get('display_name') or '').strip()
        if name:
            author_names.append(name)

    src = (work.get('primary_location') or {}).get('source') or {}
    publication = src.get('display_name')

    return {
        'title':       title,
        'year':        str(pub_year) if pub_year else None,
        'authors':     author_names,
        'publication': publication,
        'cited_by':    {'value': work.get('cited_by_count') or 0},
    }


def merge_paper_metadata(
    scholar_article: Dict,
    crossref_extras: Dict,
    crossref_issns: List[str],
    openalex_work: Optional[Dict],
) -> Dict:
    """
    Cross-source merge with explicit priority rules. Returns a unified record
    consumable by the persistence layer.

    Priority by field (highest → lowest):
        venue (journal name)  : OpenAlex > Scholar.publication
        ISSNs                 : union of OpenAlex + CrossRef (more = better journal match)
        abstract              : OpenAlex > CrossRef (OA is full-text, CR is truncated)
        biblio (vol/iss/pgs)  : OpenAlex > CrossRef
        language              : OpenAlex > heuristic detect_language()
        pub_year              : Scholar > OpenAlex (Scholar's year is what was scraped)
        source_tag            : 'Both' iff openalex_work present, else 'Scholar'

    Why "OpenAlex > CrossRef" for biblio? Because OpenAlex normalizes biblio
    aggressively across publishers; CrossRef returns whatever the publisher
    submitted (often inconsistent volume/issue formatting).
    """
    has_openalex = openalex_work is not None

    merged: Dict = {
        "venue":      scholar_article.get('publication') or 'Unknown Venue',
        "issns":      list(crossref_issns),
        "extras": {
            "volume":   crossref_extras.get('volume'),
            "issue":    crossref_extras.get('issue'),
            "pages":    crossref_extras.get('pages'),
            "abstract": crossref_extras.get('abstract'),
            "language": crossref_extras.get('language'),
        },
        "pub_year":   safe_int(scholar_article.get('year')),
        "source_tag": 'Scholar',
    }

    if has_openalex:
        oa_extras = parse_openalex_extras(openalex_work)
        oa_venue, oa_issns = parse_openalex_journal(openalex_work)

        if oa_venue:
            merged["venue"] = oa_venue

        seen = set()
        merged_issns: List[str] = []
        for issn in (oa_issns + crossref_issns):
            if issn and issn not in seen:
                seen.add(issn)
                merged_issns.append(issn)
        merged["issns"] = merged_issns

        merged["extras"] = {
            "volume":   oa_extras["volume"]   or crossref_extras.get('volume'),
            "issue":    oa_extras["issue"]    or crossref_extras.get('issue'),
            "pages":    oa_extras["pages"]    or crossref_extras.get('pages'),
            "abstract": oa_extras["abstract"] or crossref_extras.get('abstract'),
            "language": oa_extras["language"] or crossref_extras.get('language'),
        }

        if not merged["pub_year"]:
            merged["pub_year"] = safe_int(openalex_work.get('publication_year'))

        merged["source_tag"] = 'Both'

    return merged


@contextmanager
def db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def find_user_by_scholar_id(cur, scholar_id: str) -> Optional[int]:
    """Direct lookup since Users table has Scholar_ID column."""
    cur.execute(
        'SELECT "UserID" FROM "Users" WHERE "Scholar_ID" = %s',
        (scholar_id,)
    )
    res = cur.fetchone()
    return res[0] if res else None


def auto_create_researcher(cur, full_name: str, scholar_id: str) -> int:
    """
    Bootstrap a Users + Researcher row when scraping a profile that
    isn't yet wired through the proper RegistrationRequests flow.
    For testing only — production should require an approved request.
    """
    first, middle, last = split_full_name(full_name)
    placeholder_email = f"scholar.{scholar_id.lower()}@litrix.placeholder"

    cur.execute('''
        INSERT INTO "Users" (
            "FirstName", "MiddleName", "LastName",
            "Email", "UserType", "AccountStatus",
            "Scholar_ID", "CreatedAt"
        )
        VALUES (%s, %s, %s, %s, 'Researcher', 'Active', %s, NOW())
        RETURNING "UserID"
    ''', (first, middle, last, placeholder_email, scholar_id))
    user_id = cur.fetchone()[0]

    cur.execute(
        'UPDATE "Users" SET "Litrix_ID" = %s WHERE "UserID" = %s',
        (f"LIT-{user_id:06d}", user_id)
    )

    profile_url = f"https://scholar.google.com/citations?user={scholar_id}"
    cur.execute('''
        INSERT INTO "Researcher" ("UserID", "GoogleScholar_URL", "IsSurveyCompleted")
        VALUES (%s, %s, false)
        ON CONFLICT ("UserID") DO UPDATE
            SET "GoogleScholar_URL" = EXCLUDED."GoogleScholar_URL"
    ''', (user_id, profile_url))

    logging.warning(
        f"Auto-created Users + Researcher rows (UserID={user_id}). "
        "In production this should come from an approved RegistrationRequest."
    )
    return user_id


def find_user_by_normalized_name(cur, normalized: str) -> Tuple[Optional[int], float]:
    """Disambiguate a co-author against registered Users."""
    if not normalized:
        return None, 0.0
    cur.execute('''
        SELECT "UserID" FROM "Users"
        WHERE LOWER(TRIM(REGEXP_REPLACE(
            COALESCE("FirstName",'') || ' ' ||
            COALESCE("MiddleName",'') || ' ' ||
            COALESCE("LastName",''),
            '[\\.,]', '', 'g'
        ))) = %s
        LIMIT 1
    ''', (re.sub(r'\s+', ' ', normalized).strip(),))
    res = cur.fetchone()
    return (res[0], 1.0) if res else (None, 0.0)


def find_user_by_orcid(cur, orcid: str) -> Optional[int]:
    """
    Strongest possible co-author resolution. ORCID is globally unique per
    researcher, so a hit here is unambiguous (confidence = 1.0).
    """
    if not orcid:
        return None
    cur.execute('''
        SELECT u."UserID"
        FROM "Users" u
        JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE r."ORCID_ID" = %s
        LIMIT 1
    ''', (orcid,))
    res = cur.fetchone()
    return res[0] if res else None


def find_user_by_openalex_id(cur, openalex_id: str) -> Optional[int]:
    """
    Secondary canonical lookup. OpenAlex Author IDs are stable per author
    in OpenAlex's graph and are populated automatically as we scrape.
    """
    if not openalex_id:
        return None
    cur.execute('''
        SELECT u."UserID"
        FROM "Users" u
        JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE r."OpenAlex_AuthorID" = %s
        LIMIT 1
    ''', (openalex_id,))
    res = cur.fetchone()
    return res[0] if res else None


def update_researcher_last_synced(cur, user_id: int) -> None:
    """
    Stamp Researcher.LastSyncedAt = NOW() after a successful sync.
    The batch orchestrator uses this column to skip recently-synced
    researchers and to resume after a partial-batch failure.
    """
    cur.execute(
        'UPDATE "Researcher" SET "LastSyncedAt" = NOW() WHERE "UserID" = %s',
        (user_id,)
    )


def backfill_researcher_identity(cur, user_id: int,
                                 orcid: Optional[str],
                                 openalex_id: Optional[str]) -> bool:
    """
    Progressive Identity Enrichment: when OpenAlex returns ORCID and/or
    OpenAlex_AuthorID for one of OUR researchers (matched via name during
    a paper scrape), persist those identifiers so future scrapes can use
    them as primary keys instead of relying on fuzzy name matching.

    Uses COALESCE to NEVER overwrite a value that's already set — once an
    identifier is bound to a researcher, it should stay bound.

    Returns True if any field was actually written (i.e., at least one was
    NULL before this call).
    """
    if not user_id or (not orcid and not openalex_id):
        return False

    cur.execute('''
        UPDATE "Researcher"
        SET "ORCID_ID"          = COALESCE("ORCID_ID",          %s),
            "OpenAlex_AuthorID" = COALESCE("OpenAlex_AuthorID", %s)
        WHERE "UserID" = %s
          AND ("ORCID_ID" IS NULL OR "OpenAlex_AuthorID" IS NULL)
    ''', (orcid, openalex_id, user_id))

    return cur.rowcount > 0


def upsert_journal(cur, venue: str, issns: List[str]) -> int:
    """
    Get-or-create a Journal. Match priority:
        1. ISSN_Print or ISSN_Online
        2. JournalName (case-insensitive)
        3. Insert new
    NEVER overwrites a valid existing ISSN with NULL.
    """
    venue = (venue or "Unknown Venue").strip()[:500]
    issns = [i for i in issns if i]
    venue_type = determine_venue_type(venue)

    if issns:
        cur.execute('''
            SELECT "JournalID" FROM "Journals"
            WHERE "ISSN_Print" = ANY(%s) OR "ISSN_Online" = ANY(%s)
            LIMIT 1
        ''', (issns, issns))
        res = cur.fetchone()
        if res:
            return res[0]

    cur.execute(
        'SELECT "JournalID", "ISSN_Print" FROM "Journals" '
        'WHERE "JournalName" ILIKE %s LIMIT 1',
        (venue,)
    )
    res = cur.fetchone()
    if res:
        journal_id, existing_issn = res
        if not existing_issn and issns:
            cur.execute(
                'UPDATE "Journals" SET "ISSN_Print" = %s WHERE "JournalID" = %s',
                (issns[0], journal_id)
            )
        return journal_id

    main_issn = issns[0] if issns else None
    cur.execute('''
        INSERT INTO "Journals" ("JournalName", "ISSN_Print", "VenueType")
        VALUES (%s, %s, %s)
        RETURNING "JournalID"
    ''', (venue, main_issn, venue_type))
    return cur.fetchone()[0]


def link_journal_ranking(cur, journal_id: int, issns: List[str]) -> None:
    """
    Link the journal to its Scimago ranking row in JournalRankings.

    Flow:
        1. Skip if this journal already has any linked ranking
           (Scimago has multiple rows per journal — one per category).
        2. Look up the FIRST matching RankingID via ISSN_Mapping.
        3. UPDATE only if the row is unlinked AND no conflicting
           (JournalID, RankingYear) row exists.

    Honors the unique_journal_year constraint without crashing the
    transaction when Scimago contains multiple category rows per journal.
    """
    if not issns:
        return

    cur.execute(
        'SELECT 1 FROM "JournalRankings" WHERE "JournalID" = %s LIMIT 1',
        (journal_id,)
    )
    if cur.fetchone():
        return

    cur.execute('''
        SELECT "RankingID" FROM "ISSN_Mapping"
        WHERE "ISSN" = ANY(%s)
        LIMIT 1
    ''', (issns,))
    res = cur.fetchone()
    if not res:
        return

    ranking_id = res[0]

    cur.execute('''
        UPDATE "JournalRankings" jr
        SET "JournalID" = %s
        WHERE jr."RankingID" = %s
          AND jr."JournalID" IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM "JournalRankings" jr2
              WHERE jr2."JournalID"   = %s
                AND jr2."RankingYear" = jr."RankingYear"
          )
    ''', (journal_id, ranking_id, journal_id))


def find_paper(cur, doi: Optional[str], normalized_title: str) -> Optional[int]:
    """Two-stage dedup: DOI > NormalizedTitle."""
    if doi:
        cur.execute(
            'SELECT "PaperID" FROM "ResearchPaper" WHERE "DOI" = %s',
            (doi,)
        )
        res = cur.fetchone()
        if res:
            return res[0]
    if normalized_title:
        cur.execute(
            'SELECT "PaperID" FROM "ResearchPaper" WHERE "NormalizedTitle" = %s',
            (normalized_title,)
        )
        res = cur.fetchone()
        if res:
            return res[0]
    return None


def update_paper_if_empty(cur, paper_id: int, merged: Dict,
                          has_openalex: bool) -> bool:
    """
    Backfill empty fields on an EXISTING paper using freshly-fetched data.
    Two enrichment opportunities are exploited here:

        1. NULL backfill — Abstract / Volume / Issue / Pages / Language are
           filled when they were NULL in the DB and we now have a value.
        2. Source bump   — a 'Scholar'-only paper becomes 'Both' the moment
           OpenAlex confirms it (cross-source validation signal).

    NEVER overwrites existing values (COALESCE pattern).

    Defensive WHERE clause: the UPDATE is skipped entirely when nothing would
    actually change, so cur.rowcount > 0 is a reliable "was enriched" signal
    we can surface to the operator via logging.
    """
    if not has_openalex:
        return False

    extras = merged.get("extras") or {}
    params = {
        "abstract": extras.get("abstract"),
        "volume":   extras.get("volume"),
        "issue":    extras.get("issue"),
        "pages":    extras.get("pages"),
        "language": extras.get("language"),
        "paper_id": paper_id,
    }

    cur.execute('''
        UPDATE "ResearchPaper"
        SET "Abstract" = COALESCE("Abstract", %(abstract)s),
            "Volume"   = COALESCE("Volume",   %(volume)s),
            "Issue"    = COALESCE("Issue",    %(issue)s),
            "Pages"    = COALESCE("Pages",    %(pages)s),
            "Language" = COALESCE("Language", %(language)s),
            "Source"   = CASE
                WHEN "Source" = 'Scholar' THEN 'Both'
                ELSE "Source"
            END
        WHERE "PaperID" = %(paper_id)s
          AND (
            ("Abstract" IS NULL AND %(abstract)s IS NOT NULL) OR
            ("Volume"   IS NULL AND %(volume)s   IS NOT NULL) OR
            ("Issue"    IS NULL AND %(issue)s    IS NOT NULL) OR
            ("Pages"    IS NULL AND %(pages)s    IS NOT NULL) OR
            ("Language" IS NULL AND %(language)s IS NOT NULL) OR
            "Source" = 'Scholar'
          )
    ''', params)
    return cur.rowcount > 0


def insert_paper(cur, journal_id: int, title: str, normalized: str,
                 doi: Optional[str], pub_year: Optional[int],
                 raw_data: Dict, extras: Dict,
                 source: str = 'Scholar',
                 counts_by_year: Optional[Dict] = None) -> int:
    """
    Insert a new ResearchPaper. Source tag indicates provenance:
        'Scholar'  — found only via Google Scholar (no DOI match in OpenAlex)
        'OpenAlex' — found only via OpenAlex (future: backfill from OA query)
        'Both'     — present in both Scholar AND OpenAlex (cross-validated)

    counts_by_year: optional dict like {"2024": 15, "2025": 30} stored as
    JSONB in CitationsByYear. Lets the dashboard answer "citations
    received in YEAR X" instead of cumulative.
    """
    language = extras.get('language') or detect_language(title)
    cur.execute('''
        INSERT INTO "ResearchPaper" (
            "JournalID", "Title", "Title_En", "NormalizedTitle",
            "Abstract", "Language", "DOI", "PubYear",
            "Volume", "Issue", "Pages",
            "IsVerified", "ScrapedAt", "Source", "RawData_Log",
            "CitationsByYear"
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                FALSE, NOW(), %s, %s, %s)
        RETURNING "PaperID"
    ''', (
        journal_id,
        title,
        title if language == 'en' else None,
        normalized,
        extras.get('abstract'),
        language,
        doi,
        pub_year,
        extras.get('volume'),
        extras.get('issue'),
        extras.get('pages'),
        source,
        Json(raw_data),
        Json(counts_by_year) if counts_by_year else None,
    ))
    return cur.fetchone()[0]


def link_user_to_paper(cur, user_id: int, paper_id: int, order: int,
                       confidence: float, criteria: str,
                       raw_name: Optional[str] = None) -> None:
    """
    Link a registered User to a paper with full disambiguation provenance.
    Uses Authors.AuthorNameRaw to keep the original scraped name for audit.
    """
    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, %s, FALSE, %s, %s, %s, FALSE)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (user_id, paper_id, order, confidence, criteria, raw_name))


def insert_external_author(cur, full_name: str, paper_id: int,
                           affiliation: Optional[str] = None) -> Optional[int]:
    """
    ExternalAuthors is paper-scoped per the schema. We pass affiliation when
    OpenAlex provides it (Scholar doesn't), enriching co-author records that
    couldn't be matched to a registered Researcher.

    On conflict (same name, same paper) we still UPDATE the affiliation if
    we now have one and didn't before — institutional info added later.
    """
    if not full_name:
        return None
    cur.execute('''
        INSERT INTO "ExternalAuthors" ("FullName", "Affiliation", "PaperID")
        VALUES (%s, %s, %s)
        ON CONFLICT ("FullName", "PaperID") DO UPDATE
            SET "Affiliation" = COALESCE("ExternalAuthors"."Affiliation",
                                         EXCLUDED."Affiliation")
        RETURNING "ExtAuthorID"
    ''', (full_name[:255], affiliation, paper_id))
    res = cur.fetchone()
    return res[0] if res else None


def _resolve_coauthor(cur, author_record: Dict) -> Tuple[Optional[int], float, Optional[str]]:
    """
    Disambiguation pipeline for a single co-author. Returns:
        (matched_user_id, confidence, criteria) — all None if no match.

    Resolution priority (strongest → weakest):
        1. ORCID match              → confidence = 1.0  (globally unique)
        2. OpenAlex_AuthorID match  → confidence = 1.0  (canonical in OA graph)
        3. Normalized name match    → confidence = 0.7  (prone to homonyms)

    Confidence is dropped to 0.7 for name-only matches because Saudi names in
    particular have heavy duplication (multiple "Mohammed Alghamdi" in the
    same college). Anything below 1.0 is flagged for manual review later via
    Authors.MappingConfidence.
    """
    orcid = author_record.get('orcid')
    if orcid:
        uid = find_user_by_orcid(cur, orcid)
        if uid:
            return uid, 1.0, 'ORCID_match'

    oa_id = author_record.get('openalex_id')
    if oa_id:
        uid = find_user_by_openalex_id(cur, oa_id)
        if uid:
            return uid, 1.0, 'OpenAlex_AuthorID_match'

    name = author_record.get('name') or ''
    normalized = normalize_person_name(name)
    if normalized:
        uid, _ = find_user_by_normalized_name(cur, normalized)
        if uid:
            return uid, 0.7, 'Normalized_Name_Match'

    return None, 0.0, None


def process_article(cur, primary_user_id: int, primary_name: str,
                    article: Dict,
                    *,
                    prefetched_openalex_work: Optional[Dict] = None) -> str:
    """
    Process a single article through the multi-source pipeline.
    Returns 'new' | 'existing' | 'enriched'.

    Two entry modes:
      • Scholar-primary (default): fetch CrossRef → OpenAlex by DOI.
        Used by run_full_sync() when SerpAPI is the entry point.

      • OpenAlex-primary (prefetched_openalex_work set): the caller already
        has the OpenAlex work, so we skip CrossRef and the redundant
        OpenAlex DOI lookup. Used by run_full_sync_via_orcid() to avoid
        duplicate API calls.

    Flow:
      1. CrossRef + OpenAlex (Scholar-primary)  OR  use prefetched work
      2. Merge sources                          → unified record
      3. Two-stage dedup (DOI > NormalizedTitle)
      4. Persist paper + journal + ranking link
      5. Backfill primary's ORCID/OpenAlex_AuthorID if discovered
      6. Link co-authors with ORCID-first resolution
    """
    title = (article.get('title') or 'Untitled').strip()
    normalized = normalize_title(title)

    if prefetched_openalex_work is not None:
        openalex_work = prefetched_openalex_work
        doi = _normalize_doi(openalex_work.get('doi') or '') or None
        issns_cr: List[str] = []
        extras_cr: Dict = {}
    else:
        doi, issns_cr, extras_cr = fetch_crossref_metadata(title)
        openalex_work = fetch_openalex_work_by_doi(doi) if doi else None

    merged = merge_paper_metadata(article, extras_cr, issns_cr, openalex_work)

    paper_id = find_paper(cur, doi, normalized)
    is_new = paper_id is None
    was_enriched = False

    if is_new:
        journal_id = upsert_journal(cur, merged["venue"], merged["issns"])
        link_journal_ranking(cur, journal_id, merged["issns"])
        counts_by_year = None
        if openalex_work:
            cby_list = openalex_work.get('counts_by_year') or []
            if cby_list:
                counts_by_year = {
                    str(item['year']): int(item.get('cited_by_count') or 0)
                    for item in cby_list
                    if item.get('year') is not None
                }
        paper_id = insert_paper(
            cur, journal_id, title, normalized, doi,
            merged["pub_year"], article, merged["extras"],
            source=merged["source_tag"],
            counts_by_year=counts_by_year,
        )
    else:
        was_enriched = update_paper_if_empty(
            cur, paper_id, merged, has_openalex=(openalex_work is not None)
        )
        if was_enriched:
            logging.info(
                f"    ↳ enriched existing PaperID={paper_id} from OpenAlex"
            )

    if openalex_work:
        author_records = parse_openalex_authorships(openalex_work)
    else:
        author_records = [
            {
                "name":        n,
                "orcid":       None,
                "openalex_id": None,
                "affiliation": None,
                "position":    None,
            }
            for n in parse_coauthors(article)
        ]

    primary_norm = normalize_person_name(primary_name)
    primary_position = 1
    primary_oa_record: Optional[Dict] = None
    for idx, rec in enumerate(author_records, start=1):
        if normalize_person_name(rec["name"]) == primary_norm:
            primary_position = idx
            primary_oa_record = rec
            break

    if primary_oa_record and (primary_oa_record.get('orcid')
                              or primary_oa_record.get('openalex_id')):
        backfilled = backfill_researcher_identity(
            cur, primary_user_id,
            orcid=primary_oa_record.get('orcid'),
            openalex_id=primary_oa_record.get('openalex_id'),
        )
        if backfilled:
            logging.info(
                f"    ↳ enriched Researcher #{primary_user_id} with "
                f"ORCID={primary_oa_record.get('orcid')} "
                f"OAID={primary_oa_record.get('openalex_id')}"
            )

    link_user_to_paper(
        cur, primary_user_id, paper_id, primary_position,
        confidence=1.0, criteria='Scholar_ID_match',
        raw_name=primary_name,
    )

    for idx, rec in enumerate(author_records, start=1):
        if idx == primary_position:
            continue

        co_name = (rec.get('name') or '').strip()
        if not co_name:
            continue

        matched_uid, confidence, criteria = _resolve_coauthor(cur, rec)
        if matched_uid and matched_uid != primary_user_id:
            link_user_to_paper(
                cur, matched_uid, paper_id, idx,
                confidence=confidence, criteria=criteria,
                raw_name=co_name,
            )
        else:
            insert_external_author(
                cur, co_name, paper_id,
                affiliation=rec.get('affiliation'),
            )

    if is_new:
        return 'new'
    return 'enriched' if was_enriched else 'existing'


def run_full_sync(scholar_id: str) -> Optional[Dict[str, int]]:
    """
    Run a full Scholar→OpenAlex sync for one researcher identified by
    Scholar_ID. Returns the stats dict on success, or None on early failure
    (SerpAPI down, no articles found, etc.).

    The return value is what the batch orchestrator uses to decide whether
    to mark this researcher as 'completed' or 'failed' in its run report.
    """
    logging.info(f"=== Starting full sync for Scholar ID: {scholar_id} ===")

    try:
        author_name, articles = fetch_scholar_profile(scholar_id)
    except Exception as e:
        logging.error(f"Fatal SerpAPI error: {e}")
        return None

    if not articles:
        logging.warning("No articles returned from SerpAPI.")
        return None

    with db_connection() as conn:
        with conn.cursor() as cur:
            primary_user_id = find_user_by_scholar_id(cur, scholar_id)
            if primary_user_id is None:
                primary_user_id = auto_create_researcher(
                    cur, author_name, scholar_id
                )
            conn.commit()
            logging.info(
                f"Primary Researcher resolved: UserID={primary_user_id} "
                f"(name='{author_name}')"
            )

        stats = {"new": 0, "existing": 0, "enriched": 0, "error": 0}
        for art in articles:
            try:
                with conn.cursor() as cur:
                    status = process_article(
                        cur, primary_user_id, author_name, art
                    )
                    stats[status] += 1
                conn.commit()
                logging.info(f"  [{status}] {art.get('title','')[:60]}")
            except Exception as e:
                conn.rollback()
                stats["error"] += 1
                logging.error(
                    f"  [error] {art.get('title','')[:50]} :: {e}"
                )
            time.sleep(random.uniform(0.4, 1.0))

        with conn.cursor() as cur:
            update_researcher_last_synced(cur, primary_user_id)
        conn.commit()

    logging.info(f"=== Sync complete: {stats} ===")
    return stats


def _sync_via_openalex_id(user_id: int, openalex_author_id: str,
                           label: str) -> Optional[Dict[str, int]]:
    """
    Shared OpenAlex-primary sync helper used by all alternative-source
    orchestrators (ORCID, Scopus, RG name search). Once we've resolved
    the researcher to an OpenAlex Author ID, the rest of the flow is
    identical:

        1. Fetch all works (paginated)
        2. For each work, call process_article with prefetched_openalex_work
           so we avoid redundant CrossRef + OpenAlex DOI lookups.
        3. Cache the OpenAlex Author ID on the Researcher row.
        4. Stamp LastSyncedAt.

    `label` is just a string for log readability ('ORCID:0000-...',
    'Scopus:5980...', 'RG:Reem-Aljoufi'). It doesn't affect logic.
    """
    try:
        author_name, works = fetch_openalex_author_works(openalex_author_id)
    except Exception as e:
        logging.error(f"Fatal OpenAlex error for {label}: {e}")
        return None

    if not works:
        logging.warning(f"No works returned from OpenAlex for {label}")
        return None

    logging.info(
        f"OpenAlex returned {len(works)} works for '{author_name}' "
        f"(UserID={user_id}, {label})"
    )

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE "Researcher"
                SET "OpenAlex_AuthorID" = COALESCE("OpenAlex_AuthorID", %s)
                WHERE "UserID" = %s
            ''', (openalex_author_id, user_id))
        conn.commit()

        stats = {"new": 0, "existing": 0, "enriched": 0, "error": 0}
        for work in works:
            article = openalex_work_to_article_shape(work)
            try:
                with conn.cursor() as cur:
                    status = process_article(
                        cur, user_id, author_name, article,
                        prefetched_openalex_work=work,
                    )
                    stats[status] += 1
                conn.commit()
                logging.info(f"  [{status}] {article['title'][:60]}")
            except Exception as e:
                conn.rollback()
                stats["error"] += 1
                logging.error(f"  [error] {article['title'][:50]} :: {e}")
            time.sleep(random.uniform(0.4, 1.0))

        with conn.cursor() as cur:
            update_researcher_last_synced(cur, user_id)
        conn.commit()

    logging.info(f"=== OpenAlex sync complete ({label}): {stats} ===")
    return stats


def run_full_sync_via_orcid(orcid: str) -> Optional[Dict[str, int]]:
    """
    OpenAlex-primary sync for researchers identified by ORCID.

    Confidence: 1.0 (ORCID is globally unique; OpenAlex's mapping is
    deterministic via the orcid: prefix lookup).
    """
    logging.info(f"=== Starting OpenAlex sync for ORCID: {orcid} ===")
    with db_connection() as conn:
        with conn.cursor() as cur:
            user_id = find_user_by_orcid(cur, orcid)
    if user_id is None:
        logging.error(
            f"No registered Researcher with ORCID '{orcid}'. "
            "Make sure bootstrap_faculty.py ran with the latest CSV."
        )
        return None
    return _sync_via_openalex_id(user_id, orcid, label=f"ORCID:{orcid}")


def run_full_sync_via_scopus(scopus_id: str) -> Optional[Dict[str, int]]:
    """
    OpenAlex-primary sync for researchers whose only identifier is a
    Scopus Author ID. Resolves Scopus → OpenAlex first, then runs the
    standard OpenAlex pipeline.

    Confidence: 1.0 — Scopus IDs are globally unique and OpenAlex
    maintains a curated mapping.
    """
    logging.info(f"=== Starting Scopus sync for ID: {scopus_id} ===")

    oaid = find_openalex_author_by_scopus(scopus_id)
    if not oaid:
        logging.error(
            f"OpenAlex couldn't resolve Scopus ID '{scopus_id}'. "
            "The author may not be in OpenAlex's index yet."
        )
        return None
    logging.info(f"Resolved Scopus '{scopus_id}' → OpenAlex '{oaid}'")

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT u."UserID"
                FROM "Users" u
                JOIN "Researcher" r ON r."UserID" = u."UserID"
                WHERE r."Scopus_ID" = %s
                LIMIT 1
            ''', (scopus_id,))
            res = cur.fetchone()
            user_id = res[0] if res else None

    if user_id is None:
        logging.error(f"No registered Researcher with Scopus_ID '{scopus_id}'")
        return None

    return _sync_via_openalex_id(user_id, oaid, label=f"Scopus:{scopus_id}")


LITRIX_INSTITUTION_KEYWORDS = ["Al-Baha", "Al Baha", "Albaha", "Bahah"]

RG_MAX_WORKS_THRESHOLD = 200

RG_MIN_CONFIDENCE = 0.7


def run_full_sync_via_rg(rg_url: str) -> Optional[Dict[str, int]]:
    """
    Last-resort sync for researchers whose only identifier is a
    ResearchGate URL. ResearchGate has no public API, so we:

        1. Extract the English name from the RG profile slug.
        2. Search OpenAlex with a layered defense:
              - country=SA filter
              - institution-name keyword match (Al-Baha)
              - works-count sanity threshold
              - require EXACTLY ONE candidate after filters
        3. Refuse the match if confidence < RG_MIN_CONFIDENCE (0.7).

    Better to scrape ZERO papers than to attribute the wrong 1400 papers
    to the wrong researcher.
    """
    logging.info(f"=== Starting RG-based sync for: {rg_url} ===")

    name = extract_name_from_rg_url(rg_url)
    if not name:
        logging.error(f"Couldn't parse a name from RG URL: {rg_url}")
        return None
    logging.info(f"Parsed name from RG: '{name}'")

    match = find_openalex_author_by_name(
        name,
        country_code="SA",
        institution_keywords=LITRIX_INSTITUTION_KEYWORDS,
        max_works_threshold=RG_MAX_WORKS_THRESHOLD,
    )
    if not match:
        logging.warning(
            f"  ✗ no safe match for '{name}' — skipping. "
            f"This is intentional: ambiguous matches are refused to "
            f"prevent false attributions."
        )
        return None
    oaid, confidence = match

    if confidence < RG_MIN_CONFIDENCE:
        logging.warning(
            f"  ✗ Match confidence {confidence} < threshold "
            f"{RG_MIN_CONFIDENCE} for '{name}'. Refusing to bind."
        )
        return None

    logging.info(
        f"Resolved RG '{name}' → OpenAlex '{oaid}' "
        f"(confidence={confidence}) ✓ passes safety thresholds"
    )

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT u."UserID"
                FROM "Users" u
                JOIN "Researcher" r ON r."UserID" = u."UserID"
                WHERE r."ResearchGate_URL" = %s
                LIMIT 1
            ''', (rg_url,))
            res = cur.fetchone()
            user_id = res[0] if res else None

    if user_id is None:
        logging.error(f"No registered Researcher with ResearchGate_URL='{rg_url}'")
        return None

    return _sync_via_openalex_id(
        user_id, oaid,
        label=f"RG:{name} (conf={confidence})"
    )


if __name__ == "__main__":
    import sys
    scholar_id = sys.argv[1] if len(sys.argv) > 1 else "0N86D0QAAAAJ"
    run_full_sync(scholar_id)
