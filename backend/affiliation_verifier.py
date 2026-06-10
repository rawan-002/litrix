"""
============================================================================
LITRIX AFFILIATION VERIFIER
============================================================================
Multi-tier verification tool that determines whether each paper in
ResearchPaper was actually authored under Al-Baha University affiliation
(vs. attributed to an Al-Baha researcher who at the time was at a
different institution).

WHY THIS EXISTS
---------------
The Scholar scraper imports every paper from a researcher's profile —
including papers they wrote BEFORE joining Al-Baha (PhD work elsewhere,
postdoc at other institutions, sabbatical/visiting positions).

For NCAAA reporting and accurate research-output metrics, the dashboard
must show ONLY papers actually authored under Al-Baha affiliation. This
script identifies and labels each paper.

ARCHITECTURE
------------
Four cascading verification tiers (cheapest first, fallback chain):

    ┌───────────────────────────────────────────────────────────────┐
    │ Tier 1: OpenAlex API by DOI                                   │
    │   → reads authorships[].institutions[].display_name + ROR ID  │
    │   → free, 100k req/day, ~95% coverage of post-2010 papers     │
    ├───────────────────────────────────────────────────────────────┤
    │ Tier 2: Crossref API by DOI (fallback)                        │
    │   → reads author[].affiliation[].name                         │
    │   → free, ~50 req/sec, broader DOI coverage                   │
    ├───────────────────────────────────────────────────────────────┤
    │ Tier 3: PDF download + first-page text scan                   │
    │   → Unpaywall API → OA PDF URL → pypdf → regex search         │
    │   → catches papers where API metadata is incomplete           │
    ├───────────────────────────────────────────────────────────────┤
    │ Tier 4: Mark as 'pending-review'                              │
    │   → human reviewer decides via /admin                         │
    └───────────────────────────────────────────────────────────────┘

DETECTION PATTERNS
------------------
Each tier searches text returned by its source for any of:
    • "Al-Baha University"   (and dash/space variants)
    • "Albaha University"    (no-dash variant)
    • "جامعة الباحة"          (Arabic)
    • "60104698"             (Scopus AF-ID — definitive)
    • "0270eb240"            (ROR ID — definitive)

CLI USAGE
---------
    # Dry-run: walks through all pending papers and reports what would
    # happen, WITHOUT touching the DB.
    python affiliation_verifier.py --dry-run

    # Apply: actually update AffiliationVerified column.
    python affiliation_verifier.py --apply

    # Limit to first N papers (for testing).
    python affiliation_verifier.py --dry-run --limit 10

    # Restrict to one source (Scholar is the main contaminator).
    python affiliation_verifier.py --apply --source Scholar

    # Run a single tier only (useful for incremental rollouts).
    python affiliation_verifier.py --apply --tier openalex

    # Resume from last paper (auto by default; --no-resume to restart).
    python affiliation_verifier.py --apply --no-resume

    # Report what's already been verified, no API calls.
    python affiliation_verifier.py --report

SAFETY
------
* All UPDATEs run inside transactions per paper. A crash mid-run never
  leaves the DB in an inconsistent state.
* Rate-limiting honors each API's stated quota (0.1s between calls).
* Network failures DO NOT mark a paper as 'not-Al-Baha' — they leave it
  NULL so a retry can pick it up.
* The script is idempotent: re-running on already-verified papers skips
  them by default (use --re-verify to override).
============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not installed. Run: pip install requests")
    sys.exit(1)


# ============================================================================
# CONFIG
# ============================================================================

# DB connection priority: env var DATABASE_URL > Django settings.
# Stored as kwargs (cleaner than DSN string — avoids quoting issues with
# passwords containing special chars, and lets us handle empty PORT properly).
DB_KWARGS: dict[str, Any] = {}
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    # psycopg2 accepts a URL string directly via connect(dsn=URL)
    DB_KWARGS = {'dsn': DATABASE_URL}
else:
    # Load from Django settings
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')
        import django
        django.setup()
        from django.conf import settings
        db = settings.DATABASES['default']
        DB_KWARGS = {
            'host':     db['HOST'],
            'port':     int(db.get('PORT') or 5432),
            'dbname':   db['NAME'],
            'user':     db['USER'],
            'password': db['PASSWORD'],
        }
        # Forward SSL mode if Django config specified it (Neon requires it)
        opts = db.get('OPTIONS', {}) or {}
        if opts.get('sslmode'):
            DB_KWARGS['sslmode'] = opts['sslmode']
    except Exception as e:
        print('ERROR: Could not load DB config. Set DATABASE_URL env var.')
        print(f'Detail: {e}')
        sys.exit(1)

# Contact for OpenAlex/Crossref "polite pool" (faster + more reliable)
CONTACT_EMAIL = os.environ.get('LITRIX_CONTACT_EMAIL', 'ra20awn@gmail.com')
USER_AGENT = f'Litrix-AffiliationVerifier/1.0 (mailto:{CONTACT_EMAIL})'

# Rate limits (seconds between requests per API)
OPENALEX_DELAY = 0.1   # 10 req/sec — well under the 10/sec limit
CROSSREF_DELAY = 0.05  # 20 req/sec — under the 50/sec limit
UNPAYWALL_DELAY = 0.1
PDF_DOWNLOAD_TIMEOUT = 30
API_TIMEOUT = 15

# Al-Baha detection patterns (case-insensitive regex)
ALBAHA_NAME_PATTERNS = [
    r'al[\s\-]?baha\s+university',
    r'albaha\s+university',
    r'university\s+of\s+al[\s\-]?baha',
    r'university\s+of\s+albaha',
    r'جامعة\s*الباحة',
]
# Exact identifiers (substring match)
ALBAHA_IDENTIFIERS = [
    '60104698',   # Scopus AF-ID
    '0270eb240',  # ROR ID (case-insensitive)
]

# Logging
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    level=logging.INFO,
)
log = logging.getLogger('verifier')


# ============================================================================
# DETECTION
# ============================================================================

def detect_albaha_in_text(text: Optional[str]) -> Optional[str]:
    """
    Returns the matched substring if Al-Baha is detected, else None.
    Designed to be cheap: regex compiled once, short-circuits on first match.
    """
    if not text:
        return None
    text_lower = text.lower()
    # Check regex patterns
    for pat in ALBAHA_NAME_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            return m.group(0)
    # Check exact identifiers
    for ident in ALBAHA_IDENTIFIERS:
        if ident.lower() in text_lower:
            return ident
    return None


# ============================================================================
# TIER 1: OpenAlex API
# ============================================================================

def verify_via_openalex(doi: str) -> tuple[Optional[bool], dict[str, Any]]:
    """
    Looks up paper by DOI in OpenAlex and inspects authorships[].institutions[].
    Returns (verified, evidence):
        verified = True  → Al-Baha confirmed
        verified = False → checked & no Al-Baha (papers we should EXCLUDE)
        verified = None  → API failed (retry later, don't mark)
    """
    if not doi:
        return None, {'reason': 'no_doi'}

    # Normalize DOI (strip URL prefix if present)
    clean_doi = doi.strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if clean_doi.lower().startswith(prefix):
            clean_doi = clean_doi[len(prefix):]

    url = f'https://api.openalex.org/works/doi:{clean_doi}'
    try:
        r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=API_TIMEOUT)
        if r.status_code == 404:
            return None, {'reason': 'openalex_not_found', 'http_code': 404}
        if r.status_code != 200:
            return None, {'reason': f'openalex_http_{r.status_code}'}
        data = r.json()
    except requests.RequestException as e:
        return None, {'reason': 'openalex_network', 'detail': str(e)[:200]}
    except ValueError as e:  # JSON decode
        return None, {'reason': 'openalex_invalid_json', 'detail': str(e)[:200]}

    authorships = data.get('authorships') or []
    if not authorships:
        return None, {'tier': 'openalex', 'reason': 'openalex_no_authorships'}

    # Walk every (author × institution) pair. Track whether ANY institution
    # was present — a conclusive "not Al-Baha" requires that OpenAlex
    # actually had affiliation data to inspect. If every authorship lacks
    # institutions we return None (inconclusive) so a later tier / retry can
    # resolve it, instead of falsely excluding the paper.
    institutions_seen = 0
    for auth in authorships:
        author_name = (auth.get('author') or {}).get('display_name', '')
        for inst in auth.get('institutions') or []:
            institutions_seen += 1
            inst_name = inst.get('display_name', '') or ''
            ror = inst.get('ror', '') or ''
            country = inst.get('country_code', '') or ''
            match = detect_albaha_in_text(inst_name) or detect_albaha_in_text(ror)
            if match:
                return True, {
                    'tier': 'openalex',
                    'matched_author':      author_name,
                    'matched_institution': inst_name,
                    'ror':                 ror,
                    'country':             country,
                    'matched_substring':   match,
                }

    if institutions_seen == 0:
        return None, {'tier': 'openalex', 'reason': 'openalex_no_institutions'}

    return False, {
        'tier': 'openalex',
        'reason': 'no_albaha_in_authorships',
        'authorships_count': len(authorships),
        'institutions_seen': institutions_seen,
    }


# ============================================================================
# TIER 2: Crossref API
# ============================================================================

def verify_via_crossref(doi: str) -> tuple[Optional[bool], dict[str, Any]]:
    """Same contract as verify_via_openalex but uses Crossref's metadata."""
    if not doi:
        return None, {'reason': 'no_doi'}

    clean_doi = doi.strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if clean_doi.lower().startswith(prefix):
            clean_doi = clean_doi[len(prefix):]

    url = f'https://api.crossref.org/works/{clean_doi}'
    try:
        r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=API_TIMEOUT)
        if r.status_code == 404:
            return None, {'reason': 'crossref_not_found'}
        if r.status_code != 200:
            return None, {'reason': f'crossref_http_{r.status_code}'}
        msg = (r.json() or {}).get('message', {})
    except requests.RequestException as e:
        return None, {'reason': 'crossref_network', 'detail': str(e)[:200]}
    except ValueError as e:
        return None, {'reason': 'crossref_invalid_json', 'detail': str(e)[:200]}

    authors = msg.get('author') or []
    if not authors:
        return None, {'tier': 'crossref', 'reason': 'crossref_no_authors'}

    # Crossref very often omits affiliations entirely. So we only return a
    # conclusive "not Al-Baha" when at least one affiliation string was
    # actually present; otherwise it's inconclusive (None) and we fall
    # through to the HTML/PDF tiers instead of falsely excluding the paper.
    affiliations_seen = 0
    for auth in authors:
        author_name = f"{auth.get('given', '')} {auth.get('family', '')}".strip()
        for aff in auth.get('affiliation') or []:
            aff_name = aff.get('name', '') or ''
            if aff_name:
                affiliations_seen += 1
            match = detect_albaha_in_text(aff_name)
            if match:
                return True, {
                    'tier': 'crossref',
                    'matched_author':      author_name,
                    'matched_affiliation': aff_name,
                    'matched_substring':   match,
                }

    if affiliations_seen == 0:
        return None, {'tier': 'crossref', 'reason': 'crossref_no_affiliations'}

    return False, {
        'tier': 'crossref',
        'reason': 'no_albaha_in_affiliations',
        'authors_count': len(authors),
        'affiliations_seen': affiliations_seen,
    }


# ============================================================================
# TIER 3: PDF download + extraction
# ============================================================================

def _import_pdf_lib():
    """Lazy-import the PDF library so the verifier still runs Tier 1+2 even
    if pypdf isn't installed."""
    try:
        import pypdf  # modern fork of PyPDF2
        return pypdf
    except ImportError:
        try:
            import PyPDF2 as pypdf
            return pypdf
        except ImportError:
            return None


def verify_via_pdf(doi: str) -> tuple[Optional[bool], dict[str, Any]]:
    """
    Steps:
      1. Unpaywall API → find Open Access PDF URL for the DOI.
      2. Download PDF (max ~10MB to avoid wasted bandwidth on giant docs).
      3. Extract text from first 2 pages (affiliation is always page 1).
      4. Search for Al-Baha patterns.
    """
    if not doi:
        return None, {'reason': 'no_doi'}

    pypdf = _import_pdf_lib()
    if pypdf is None:
        return None, {'reason': 'pypdf_not_installed'}

    clean_doi = doi.strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if clean_doi.lower().startswith(prefix):
            clean_doi = clean_doi[len(prefix):]

    # Step 1: ask Unpaywall for the OA PDF URL
    try:
        unpw_url = f'https://api.unpaywall.org/v2/{clean_doi}?email={CONTACT_EMAIL}'
        r = requests.get(unpw_url, timeout=API_TIMEOUT)
        if r.status_code != 200:
            return None, {'reason': f'unpaywall_http_{r.status_code}'}
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return None, {'reason': 'unpaywall_failed', 'detail': str(e)[:200]}

    best_oa = data.get('best_oa_location') or {}
    pdf_url = best_oa.get('url_for_pdf') or best_oa.get('url')
    if not pdf_url:
        return None, {'reason': 'no_oa_pdf_available'}

    # Step 2: download (streamed, abort if too big)
    try:
        pdf_resp = requests.get(
            pdf_url,
            timeout=PDF_DOWNLOAD_TIMEOUT,
            stream=True,
            headers={'User-Agent': USER_AGENT},
            allow_redirects=True,
        )
        if pdf_resp.status_code != 200:
            return None, {'reason': f'pdf_http_{pdf_resp.status_code}', 'url': pdf_url[:200]}

        # Cap at 10 MB to protect memory
        MAX_BYTES = 10 * 1024 * 1024
        chunks = []
        downloaded = 0
        for chunk in pdf_resp.iter_content(8192):
            chunks.append(chunk)
            downloaded += len(chunk)
            if downloaded > MAX_BYTES:
                return None, {'reason': 'pdf_too_large', 'bytes': downloaded}
        pdf_bytes = b''.join(chunks)
    except requests.RequestException as e:
        return None, {'reason': 'pdf_download_failed', 'detail': str(e)[:200]}

    # Step 3: extract text from first 2 pages
    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes))
        text = ''
        for page in reader.pages[:2]:
            try:
                text += (page.extract_text() or '') + '\n'
            except Exception:
                continue
    except Exception as e:
        return None, {'reason': 'pdf_parse_failed', 'detail': str(e)[:200]}

    # Step 4: detection
    match = detect_albaha_in_text(text)
    if match:
        return True, {
            'tier': 'pdf',
            'pdf_url': pdf_url[:300],
            'matched_substring': match,
            'text_length': len(text),
        }

    return False, {
        'tier': 'pdf',
        'pdf_url': pdf_url[:300],
        'reason': 'pdf_no_albaha',
        'text_length': len(text),
    }


# ============================================================================
# TIER 4: Publisher HTML — Smart scraping with multiple strategies
# ============================================================================
# Unlike Tier 3 (PDF) which only sees the document body, this tier reads the
# RICH HTML page on the publisher's site. That page typically exposes the
# authors' affiliations via:
#
#   1. Citation meta tags  — Google Scholar standard
#      <meta name="citation_author_institution" content="Al-Baha University">
#
#   2. Dublin Core meta tags — academic publishing standard
#      <meta name="DC.contributor.affiliation" content="...">
#
#   3. JSON-LD structured data — schema.org/ScholarlyArticle
#      <script type="application/ld+json">{...}</script>
#
#   4. Publisher-specific CSS selectors
#      Springer:  .c-article-author-affiliation__address
#      IEEE:      .author-affiliation
#      Elsevier:  .author .affiliation
#      Wiley:     .author-info .affiliation
#      MDPI:      .affiliation-item
#      Frontiers: .affiliation-info
#
#   5. Full HTML text search (fallback)
#
# This tier handles the "show more authors" case the user noted: those
# affiliations ARE in the HTML, they're just visually hidden until JS unhides
# them. Reading the raw HTML bypasses the click requirement entirely.

def _search_jsonld_for_albaha(obj: Any, path: str = '$') -> Optional[dict]:
    """Walks a JSON-LD tree looking for any string mentioning Al-Baha."""
    if isinstance(obj, str):
        match = detect_albaha_in_text(obj)
        if match:
            return {'path': path, 'matched': match, 'text': obj[:200]}
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = _search_jsonld_for_albaha(v, f'{path}.{k}')
            if r:
                return r
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            r = _search_jsonld_for_albaha(item, f'{path}[{i}]')
            if r:
                return r
    return None


def verify_via_publisher_html(doi: str) -> tuple[Optional[bool], dict[str, Any]]:
    """
    Resolves DOI to the publisher's HTML page and checks affiliation info
    via the 5 strategies above (most reliable first).
    """
    if not doi:
        return None, {'reason': 'no_doi'}

    # bs4 is the parser we need — lazy import so the verifier can still run
    # the other tiers if bs4 isn't installed.
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None, {'reason': 'bs4_not_installed'}

    clean_doi = doi.strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if clean_doi.lower().startswith(prefix):
            clean_doi = clean_doi[len(prefix):]

    # doi.org follows redirects to the publisher's landing page
    url = f'https://doi.org/{clean_doi}'
    try:
        r = requests.get(
            url,
            allow_redirects=True,
            timeout=API_TIMEOUT * 2,  # publishers can be slow
            headers={
                'User-Agent': USER_AGENT,
                # Some publishers gate-keep on Accept; pretend to be a normal browser
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
            },
        )
        if r.status_code != 200:
            return None, {'reason': f'html_http_{r.status_code}', 'url': r.url[:200]}
        html = r.text
        final_url = r.url
    except requests.RequestException as e:
        return None, {'reason': 'html_network', 'detail': str(e)[:200]}

    # Catch obvious dead-ends early (Cloudflare challenge, captcha, etc.)
    if len(html) < 500:
        return None, {'reason': 'html_too_short', 'url': final_url[:200], 'length': len(html)}

    try:
        # lxml is faster than html.parser but falls back gracefully
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')
    except Exception as e:
        return None, {'reason': 'html_parse_failed', 'detail': str(e)[:200]}

    # -------------------------------------------------------------------
    # Strategy 1: Citation meta tags (highest confidence)
    # -------------------------------------------------------------------
    meta_patterns = [
        'citation_author_institution',
        'citation_institution',
        'citation_author_affiliation',
        'citation_affiliation',
        'dc.contributor.affiliation',
        'dc.creator.affiliation',
        'prism.affiliation',
        'eprints.affiliation',
    ]
    for meta in soup.find_all('meta'):
        name = (meta.get('name', '') or meta.get('property', '') or '').lower()
        content = meta.get('content', '') or ''
        if not name or not content:
            continue
        if any(p in name for p in meta_patterns):
            match = detect_albaha_in_text(content)
            if match:
                return True, {
                    'tier': 'publisher_html',
                    'strategy': 'meta_tag',
                    'meta_name': name,
                    'matched_content': content[:200],
                    'matched_substring': match,
                    'url': final_url[:300],
                }

    # -------------------------------------------------------------------
    # Strategy 2: JSON-LD structured data
    # -------------------------------------------------------------------
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            raw = script.string or script.get_text() or ''
            if not raw.strip():
                continue
            data = json.loads(raw)
        except (json.JSONDecodeError, AttributeError):
            continue
        result = _search_jsonld_for_albaha(data)
        if result:
            return True, {
                'tier': 'publisher_html',
                'strategy': 'jsonld',
                'matched_path': result['path'],
                'matched_substring': result['matched'],
                'matched_text': result['text'],
                'url': final_url[:300],
            }

    # -------------------------------------------------------------------
    # Strategy 3: Publisher-specific CSS selectors
    # -------------------------------------------------------------------
    publisher_selectors = [
        # Springer / Nature / BMC family
        '.c-article-author-affiliation__address',
        '.c-article-author-affiliation__name',
        '.AffiliationDetailsName',
        '.AffiliationDetailsAddress',
        # IEEE Xplore
        '.author-affiliation',
        '.authors-info .author .affiliation',
        # ScienceDirect / Elsevier
        '.author .affiliation',
        '.affiliation',
        'dl.author-group dd',
        # Wiley Online
        '.loa-wrapper .article-author-info .article-author-info__affiliation',
        '.author-affiliation-info',
        # ACM Digital Library
        '.loa__author-info .author-affiliation',
        '.author-info__affiliation',
        # Frontiers
        '.affiliation-info',
        '.AuthorList--listElement .affiliation',
        # MDPI
        '.affiliation-item',
        '.art-affiliation',
        # Taylor & Francis
        '.entryAuthor .affiliation',
        # Hindawi (now Wiley)
        '.articleHeader-AuthorAffiliations',
        # Cambridge / Oxford
        '.author-info-affiliation',
        # PLOS
        '.address-line',
        # PubMed / NCBI
        '.affiliations',
        '.affil',
        # SAGE
        '.author-institution',
        '.authorLayer .aff',
        # Emerald
        '.intent_author_affiliation',
        '.rlist--inline .aff',
        # IOP Publishing
        '.wd-jnl-art-author-affiliations',
        '.affiliations-list',
        # RSC (Royal Society of Chemistry)
        '.article__author-affiliation',
        # ACS (American Chemical Society)
        '.affiliations div',
        '.loa-info-affiliations',
        # De Gruyter
        '.contributorAffiliation',
        # Karger
        '.affiliation-list',
        # ASCE / AIP / AIP-style Atypon
        '.NLM_aff',
        '.affil-text',
        # Generic schema.org / Atypon platforms (covers many hosted journals)
        '[class*="affiliation"]',
        '[id*="aff"]',
    ]
    for selector in publisher_selectors:
        try:
            elements = soup.select(selector)
        except Exception:
            continue
        for elem in elements:
            text = elem.get_text(' ', strip=True)
            if not text:
                continue
            match = detect_albaha_in_text(text)
            if match:
                return True, {
                    'tier': 'publisher_html',
                    'strategy': 'publisher_selector',
                    'selector': selector,
                    'matched_text': text[:200],
                    'matched_substring': match,
                    'url': final_url[:300],
                }

    # -------------------------------------------------------------------
    # Strategy 4: Full HTML text search (fallback — last resort)
    # We strip noisy elements first to reduce false matches on navigation,
    # ads, footers etc. CRITICALLY we also drop the references / bibliography
    # / acknowledgements / cited-by blocks: a paper that merely CITES or
    # THANKS an Al-Baha author would otherwise be mislabelled as Al-Baha.
    # -------------------------------------------------------------------
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe']):
        tag.decompose()

    # Drop reference/bibliography/acknowledgement/cited-by containers by id,
    # class, or section role — matched loosely since publishers name them
    # inconsistently (references, ref-list, bibliography, citedby, …).
    _NOISE_RE = re.compile(
        r'(reference|bibliograph|ref[-_]?list|citedby|cited-by|'
        r'acknowledg|backmatter|back-matter|footnote)',
        re.IGNORECASE,
    )
    for tag in soup.find_all(['section', 'div', 'ol', 'ul', 'aside']):
        # A matching ancestor may have already decomposed this tag (find_all
        # returns nested matches); a decomposed tag has attrs == None.
        if getattr(tag, 'attrs', None) is None:
            continue
        cls = tag.get('class') or []
        ident = ' '.join([
            tag.get('id', '') or '',
            ' '.join(cls) if isinstance(cls, list) else str(cls),
            tag.get('role', '') or '',
            tag.get('aria-label', '') or '',
        ])
        if _NOISE_RE.search(ident):
            tag.decompose()

    plain_text = soup.get_text(' ', strip=True)
    match = detect_albaha_in_text(plain_text)
    if match:
        # Locate context around the match (±80 chars) for evidence
        idx = plain_text.lower().find(match.lower())
        context = plain_text[max(0, idx - 80):idx + len(match) + 80] if idx >= 0 else ''
        return True, {
            'tier': 'publisher_html',
            'strategy': 'fulltext',
            'matched_substring': match,
            'matched_context': context[:300],
            'url': final_url[:300],
        }

    return False, {
        'tier': 'publisher_html',
        'reason': 'no_albaha_in_html',
        'url': final_url[:300],
        'html_length': len(html),
    }


# ============================================================================
# TIER ORCHESTRATION
# ============================================================================

TIERS = {
    'openalex':        verify_via_openalex,
    'crossref':        verify_via_crossref,
    'publisher_html':  verify_via_publisher_html,
    'pdf':             verify_via_pdf,
}
# Default order — Publisher HTML BEFORE PDF because it's both faster AND
# more accurate (the affiliation is rendered as structured metadata, not
# scattered inside the document body).
DEFAULT_TIER_ORDER = ['openalex', 'crossref', 'publisher_html', 'pdf']

TIER_DELAYS = {
    'openalex':       OPENALEX_DELAY,
    'crossref':       CROSSREF_DELAY,
    'publisher_html': 0.2,  # be polite — many publishers rate-limit aggressively
    'pdf':            UNPAYWALL_DELAY,
}


def verify_paper(
    paper_id: int,
    doi: Optional[str],
    source: str,
    tiers_to_run: list[str],
) -> dict[str, Any]:
    """
    Runs verification tiers in order until ONE succeeds (or all fail).
    Returns a dict ready to write to the DB:
        {
            'verified': True/False/None,
            'verification_source': 'openalex'|'crossref'|'pdf'|'pending-review',
            'details': {...evidence...},
        }
    """
    last_evidence: dict[str, Any] = {}
    conclusive_negative: Optional[dict[str, Any]] = None

    for tier_name in tiers_to_run:
        tier_fn = TIERS[tier_name]
        time.sleep(TIER_DELAYS[tier_name])

        # A bug or unexpected payload in one tier must never abort the whole
        # run — treat any unhandled error as inconclusive and move on.
        try:
            verified, evidence = tier_fn(doi or '')
        except Exception as e:
            verified, evidence = None, {
                'tier': tier_name, 'reason': 'tier_exception', 'detail': str(e)[:200],
            }
        last_evidence = evidence

        if verified is True:
            return {
                'verified': True,
                'verification_source': tier_name,
                'details': evidence,
            }
        if verified is False:
            # Conclusive negative — this tier had affiliation data and none
            # of it was Al-Baha. Remember it (prefer the FIRST, most
            # authoritative tier — OpenAlex), but keep trying later tiers in
            # case their richer metadata surfaces an Al-Baha affiliation.
            if conclusive_negative is None:
                conclusive_negative = dict(evidence)
                conclusive_negative.setdefault('tier', tier_name)
            continue
        # verified is None → inconclusive (no data / network / 404), try next.

    # All tiers exhausted with no positive match. If ANY tier conclusively
    # inspected affiliation data and found no Al-Baha, mark False. Otherwise
    # leave it NULL (pending-review) so a retry / human can resolve it — a
    # purely inconclusive run must never exclude a paper.
    if conclusive_negative is not None:
        return {
            'verified': False,
            'verification_source': conclusive_negative.get('tier', 'pending-review'),
            'details': conclusive_negative,
        }
    return {
        'verified': None,
        'verification_source': 'pending-review',
        'details': last_evidence,
    }


# ============================================================================
# DB OPERATIONS
# ============================================================================

def default_verification_years() -> list[int]:
    """Dashboard-scope years, computed dynamically.

    Previously hard-coded to [2025, 2026], which silently skipped papers
    published in later years (an incremental scrape can pull next-year
    papers — Scholar lists in-press 2027 items today). Window: last year
    through next year, overridable via --years.
    """
    from datetime import date
    y = date.today().year
    return [y - 1, y, y + 1]


def fetch_pending_papers(
    conn,
    source_filter: Optional[str],
    limit: Optional[int],
    resume: bool,
    re_verify: bool,
    retry_pending: bool = False,
    years: Optional[list[int]] = None,
    user_id: Optional[int] = None,
    department_id: Optional[int] = None,
    attributed_only: bool = False,
) -> list[dict]:
    """
    Selects papers needing verification.
      - source_filter: 'Scholar' (most common), 'Manual', etc.
      - resume: skip already-verified rows (default True)
      - re_verify: include ALL papers regardless of current state
      - retry_pending: only re-process papers previously marked pending-review
      - years: PubYear scope (default: dynamic dashboard window)
      - user_id: restrict to papers authored by this Users.UserID (testing
        a single researcher's profile)
      - department_id: restrict to papers authored by a researcher whose
        CURRENT position is in this DepartmentID (department-level run —
        matches the dashboard's department scoping exactly)
    """
    where = ['1=1']
    params: list[Any] = []

    if source_filter:
        where.append('rp."Source" = %s')
        params.append(source_filter)

    if user_id is not None:
        where.append(
            'EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID" '
            'AND a."UserID" = %s)'
        )
        params.append(user_id)

    if department_id is not None:
        where.append(
            'EXISTS (SELECT 1 FROM "Authors" a '
            'JOIN "Works_In" w ON w."UserID" = a."UserID" '
            '                 AND w."IsCurrentPosition" = TRUE '
            'WHERE a."PaperID" = rp."PaperID" AND w."DepartmentID" = %s)'
        )
        params.append(department_id)

    if attributed_only:
        # Only papers linked to at least one Litrix author — these are the
        # ones that actually surface on the dashboards. Skips orphan papers.
        where.append(
            'EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")'
        )

    if retry_pending:
        # Special path: pick up only the papers Tier 3 (PDF) couldn't resolve.
        # We override resume so the loop sees them again.
        where.append('rp."VerificationSource" = %s')
        params.append('pending-review')
    elif not re_verify and resume:
        where.append('rp."AffiliationVerified" IS NULL')

    # Only consider papers in dashboard scope to avoid wasting effort
    where.append('rp."PubYear" = ANY(%s)')
    params.append(years or default_verification_years())

    sql = f'''
        SELECT
            rp."PaperID",
            rp."Title",
            rp."DOI",
            rp."Source",
            rp."AffiliationVerified",
            rp."VerificationSource"
        FROM "ResearchPaper" rp
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN rp."DOI" IS NOT NULL AND rp."DOI" <> '' THEN 0 ELSE 1 END,
            rp."PaperID"
    '''
    if limit:
        sql += f' LIMIT {int(limit)}'

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def update_paper_verification(
    conn,
    paper_id: int,
    result: dict[str, Any],
):
    """Writes the verification decision to the DB. Run inside a transaction."""
    with conn.cursor() as cur:
        cur.execute(
            '''
            UPDATE "ResearchPaper"
            SET "AffiliationVerified"  = %s,
                "VerificationSource"   = %s,
                "VerifiedAt"           = NOW(),
                "VerificationDetails"  = %s::jsonb
            WHERE "PaperID" = %s
            ''',
            [
                result['verified'],
                result['verification_source'],
                json.dumps(result['details'], ensure_ascii=False, default=str),
                paper_id,
            ],
        )


# ============================================================================
# CLI
# ============================================================================

def cmd_report(conn, years: Optional[list[int]] = None):
    """Prints per-source verification stats. No API calls."""
    yrs = years or default_verification_years()
    print()
    print('═' * 80)
    print(' VERIFICATION REPORT'.center(80))
    print(f' (PubYear scope: {", ".join(map(str, yrs))})'.center(80))
    print('═' * 80)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''
            SELECT
                rp."Source"                                              AS source,
                COUNT(DISTINCT rp."PaperID")                             AS total,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."AffiliationVerified" = TRUE)  AS verified_yes,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."AffiliationVerified" = FALSE) AS verified_no,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."AffiliationVerified" IS NULL) AS pending,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."DOI" IS NULL OR rp."DOI" = '') AS no_doi
            FROM "ResearchPaper" rp
            JOIN "Authors" a ON a."PaperID" = rp."PaperID"
            WHERE rp."PubYear" = ANY(%s)
            GROUP BY rp."Source"
            ORDER BY total DESC
        ''', [yrs])
        rows = cur.fetchall()

    print(f'\n{"Source":<15} {"Total":>7} {"Al-Baha":>10} {"Not Al-Baha":>13} {"Pending":>9} {"No DOI":>8}')
    print('-' * 80)
    totals = {'total': 0, 'verified_yes': 0, 'verified_no': 0, 'pending': 0, 'no_doi': 0}
    for r in rows:
        print(f'{r["source"]:<15} {r["total"]:>7} {r["verified_yes"]:>10} '
              f'{r["verified_no"]:>13} {r["pending"]:>9} {r["no_doi"]:>8}')
        for k in totals:
            totals[k] += r[k]
    print('-' * 80)
    print(f'{"TOTAL":<15} {totals["total"]:>7} {totals["verified_yes"]:>10} '
          f'{totals["verified_no"]:>13} {totals["pending"]:>9} {totals["no_doi"]:>8}')

    # Tier breakdown
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''
            SELECT
                "VerificationSource" AS src,
                COUNT(*)             AS n
            FROM "ResearchPaper"
            WHERE "VerificationSource" IS NOT NULL
            GROUP BY "VerificationSource"
            ORDER BY n DESC
        ''')
        tier_rows = cur.fetchall()
    if tier_rows:
        print(f'\n{"Verification Source":<25} {"Count":>8}')
        print('-' * 40)
        for r in tier_rows:
            print(f'{r["src"]:<25} {r["n"]:>8}')
    print()


def cmd_verify(conn, args):
    """The main verification loop."""
    # Determine tiers to run
    if args.tier:
        tiers_to_run = [args.tier]
    else:
        tiers_to_run = list(DEFAULT_TIER_ORDER)
    log.info(f'Tier(s) to run: {tiers_to_run}')

    # Pull pending papers
    years = None
    if getattr(args, 'years', None):
        years = [int(y.strip()) for y in args.years.split(',') if y.strip()]
    papers = fetch_pending_papers(
        conn,
        source_filter=args.source,
        limit=args.limit,
        resume=args.resume,
        re_verify=args.re_verify,
        retry_pending=args.retry_pending,
        years=years,
        user_id=getattr(args, 'user', None),
        department_id=getattr(args, 'department', None),
        attributed_only=getattr(args, 'attributed', False),
    )
    log.info(f'Fetched {len(papers)} pending papers')
    if not papers:
        log.info('Nothing to do. Run with --report to see current state.')
        return

    # Counters
    stats = {
        'processed': 0,
        'verified_yes': 0,
        'verified_no':  0,
        'pending':      0,
        'skipped_no_doi': 0,
    }

    for idx, paper in enumerate(papers, 1):
        title_preview = (paper['Title'] or '')[:60]
        doi = paper['DOI']

        # If a tier was requested but paper has no DOI, skip immediately
        if not doi or doi.strip() == '':
            log.warning(f'[{idx}/{len(papers)}] Paper #{paper["PaperID"]} — '
                        f'no DOI, skipping. Title: {title_preview}')
            stats['skipped_no_doi'] += 1
            continue

        log.info(f'[{idx}/{len(papers)}] Paper #{paper["PaperID"]} (DOI={doi[:50]})')

        result = verify_paper(
            paper_id=paper['PaperID'],
            doi=doi,
            source=paper['Source'],
            tiers_to_run=tiers_to_run,
        )

        # Update stats
        if result['verified'] is True:
            stats['verified_yes'] += 1
            log.info(f'  ✓ VERIFIED Al-Baha via {result["verification_source"]}')
        elif result['verified'] is False:
            stats['verified_no'] += 1
            log.info(f'  ✗ NOT Al-Baha via {result["verification_source"]}')
        else:
            stats['pending'] += 1
            log.warning(f'  ? PENDING ({result["details"].get("reason", "unknown")})')

        stats['processed'] += 1

        # Apply (or dry-run) the result
        if args.apply:
            try:
                update_paper_verification(conn, paper['PaperID'], result)
                conn.commit()
            except Exception as e:
                conn.rollback()
                log.error(f'  ! DB update failed: {e}')

    # Final summary
    print()
    print('═' * 80)
    print(' RUN SUMMARY'.center(80))
    print('═' * 80)
    print(f'  Processed:        {stats["processed"]}')
    print(f'  Al-Baha verified: {stats["verified_yes"]}')
    print(f'  NOT Al-Baha:      {stats["verified_no"]}')
    print(f'  Pending (retry):  {stats["pending"]}')
    print(f'  Skipped (no DOI): {stats["skipped_no_doi"]}')
    print(f'  Mode:             {"APPLY (writes to DB)" if args.apply else "DRY-RUN (no writes)"}')
    print()


def main():
    # Windows consoles default to cp1252 — wrap stdout/stderr in UTF-8 so the
    # box-drawing run summary and Arabic paper titles don't crash the run
    # (matches the repo's other pipeline scripts).
    for _stream in ('stdout', 'stderr'):
        s = getattr(sys, _stream)
        if getattr(s, 'encoding', '') and s.encoding.lower() != 'utf-8' and hasattr(s, 'buffer'):
            import io
            setattr(sys, _stream,
                    io.TextIOWrapper(s.buffer, encoding='utf-8', errors='replace'))

    parser = argparse.ArgumentParser(
        description='Litrix multi-tier affiliation verifier',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('CLI USAGE')[1].split('SAFETY')[0] if '__doc__' in dir() else '',
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='Walk through, do not write to DB')
    g.add_argument('--apply',   action='store_true', help='Write verification results to DB')
    g.add_argument('--report',  action='store_true', help='Print current verification stats only')

    parser.add_argument('--source', choices=['Scholar', 'Scopus', 'OpenAlex', 'Manual', 'Manual_EBSCO'],
                        help='Restrict to one source (default: all)')
    parser.add_argument('--tier', choices=list(TIERS.keys()),
                        help='Run a single tier only (default: all in order)')
    parser.add_argument('--limit', type=int, help='Process at most N papers (testing)')
    parser.add_argument('--user', type=int,
                        help='Restrict to papers authored by this Users.UserID')
    parser.add_argument('--department', type=int,
                        help='Restrict to papers by current researchers in this DepartmentID')
    parser.add_argument('--attributed', action='store_true',
                        help='Only papers linked to at least one Litrix author (dashboard-visible)')
    parser.add_argument('--resume',    dest='resume', action='store_true',  default=True,
                        help='Skip already-verified papers (default)')
    parser.add_argument('--no-resume', dest='resume', action='store_false',
                        help='Re-process even verified papers')
    parser.add_argument('--re-verify', action='store_true',
                        help='Re-run on ALL papers (overrides --resume)')
    parser.add_argument('--retry-pending', action='store_true',
                        help='Only retry papers that were previously marked pending-review')
    parser.add_argument('--years', type=str, default=None,
                        help='Comma-separated PubYear scope, e.g. "2025,2026,2027" '
                             '(default: last year through next year, dynamic)')

    args = parser.parse_args()

    # Open connection (kwargs avoid DSN string parsing pitfalls)
    log.info('Connecting to database...')
    try:
        conn = psycopg2.connect(connect_timeout=15, **DB_KWARGS)
    except psycopg2.Error as e:
        log.error(f'DB connection failed: {e}')
        sys.exit(1)
    log.info('Connected.')

    # Parse the optional year scope once so the report and the run agree.
    report_years = None
    if getattr(args, 'years', None):
        report_years = [int(y.strip()) for y in args.years.split(',') if y.strip()]

    try:
        if args.report:
            cmd_report(conn, report_years)
        else:
            cmd_verify(conn, args)
            if args.apply:
                cmd_report(conn, report_years)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
