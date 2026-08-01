"""Decide whether each ResearchPaper was actually authored under Al-Baha
University affiliation, vs. attributed to an Al-Baha researcher who wrote it
while at another institution.

The Scholar scraper pulls every paper off a researcher's profile, including
work from before they joined Al-Baha (PhD/postdoc elsewhere, visiting
positions). NCAAA reporting needs the dashboard to count only papers truly
authored under Al-Baha, so this labels each one.

Verification cascades through tiers (cheapest/most reliable first), stopping
at the first conclusive answer: OpenAlex by DOI, then Crossref by DOI, then
the publisher's HTML landing page, then (IEEE DOIs only) a real headless
browser render of the IEEE Xplore page, then an Unpaywall OA PDF scan.
Anything left unresolved is marked pending-review for a human. Each tier
looks for "Al-Baha University" (and dash/space/no-dash/Arabic variants) or
the definitive Scopus AF-ID / ROR identifiers.

The browser tier exists because IEEE Xplore's document page is a JS-only
Angular SPA: a plain HTTP GET gets back a bot-challenge stub (empirically:
HTTP 202, ~2KB, zero author data), so the HTML tier can never resolve it.
A real browser isn't blocked the same way, and the rendered page embeds the
full per-author affiliation array as JSON (the same data IEEE's own UI reads
to populate the on-hover author tooltip) — so no hover/click simulation is
needed, just a page load + a JSON extraction. Requires Chrome + the
`selenium` package; degrades to inconclusive (not a false negative) if
either is missing.

Two things are deliberate and easy to break: a network/API failure leaves a
paper NULL rather than marking it not-Al-Baha (a retry picks it up), and a
"not Al-Baha" verdict is only returned when a tier actually had affiliation
data to inspect — never on missing data. Per-paper transactions, polite
per-API rate limiting, and idempotent (re-runs skip verified papers unless
--re-verify). Run with --dry-run / --apply / --report; see argparse --help
for the source/tier/scope flags.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
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

# Al-Baha detection patterns (case-insensitive regex). Expanded synonym set:
# covers spacing (Al Baha / Al-Baha / Albaha), common misspellings (Bahah,
# Bahaa, Baaha), the abbreviated "Univ.", the "University of ..." word order,
# and Arabic ة/ه ending variants. Anchored on "<albaha> + univ" (or the Arabic
# جامعة) so a stray "Baha" alone never matches.
_BAHA = r'(?:baha|bahah|bahaa|baaha)'
ALBAHA_NAME_PATTERNS = [
    rf'al[\s\-]?{_BAHA}\s+univ',            # Al-Baha / Al Baha / Albaha (+typos) University/Univ.
    rf'univ\w*\s+of\s+al[\s\-]?{_BAHA}',    # University of Al-Baha
    rf'al[\s\-]?{_BAHA}\s+(?:college|faculty)',  # Al-Baha College/Faculty (some records name it so)
    r'جامع[ةه]\s*الباح[ةه]',                # جامعة الباحة (+ ة/ه variants)
    r'كلي[ةه].{0,40}الباح[ةه]',             # كلية ... الباحة
]
# Exact institutional identifiers (substring match) — the most decisive signal.
ALBAHA_IDENTIFIERS = [
    '60104698',   # Scopus Affiliation ID
    '0270eb240',  # ROR ID (ror.org/0270eb240)
    'grid.449644', # GRID ID (legacy, maps to the ROR)
]

# Verification-algorithm version, stamped into every result's details as
# `verifier_version`. Bump it whenever the decision logic changes so we can tell
# which papers were verified by an OLDER algorithm and re-verify only those (the
# resume path re-picks any paper whose stored version != this). History:
#   1.x  linear cascade, OpenAlex-first, DOI required
#   2.x  (unreleased) venue work
#   3.0.0  authority model (official decisive / supporting = evidence only),
#          no-DOI title resolution, decision_basis + evidence_trail, Strict/Perf.
VERIFIER_VERSION = '3.0.0'

# Logging
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    level=logging.INFO,
)
log = logging.getLogger('verifier')


def detect_albaha_in_text(text: Optional[str]) -> Optional[str]:
    """Return the matched substring if Al-Baha is detected, else None.
    Short-circuits on the first match to stay cheap."""
    if not text:
        return None
    text_lower = text.lower()
    for pat in ALBAHA_NAME_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            return m.group(0)
    for ident in ALBAHA_IDENTIFIERS:
        if ident.lower() in text_lower:
            return ident
    return None


# Tier 1: OpenAlex by DOI.

def verify_via_openalex(doi: str) -> tuple[Optional[bool], dict[str, Any]]:
    """Look up the DOI in OpenAlex and inspect authorships[].institutions[].
    Returns (verified, evidence) where verified is True (Al-Baha confirmed),
    False (checked, no Al-Baha — exclude), or None (API failed, retry later
    without marking)."""
    if not doi:
        return None, {'reason': 'no_doi'}

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

    # A conclusive "not Al-Baha" requires that OpenAlex actually had
    # affiliation data to inspect. If no authorship lists institutions we
    # return None (inconclusive) so a later tier / retry can resolve it,
    # rather than falsely excluding the paper.
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


# Tier 2: Crossref by DOI (fallback).

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

    # Crossref very often omits affiliations entirely, so only call it
    # conclusively "not Al-Baha" when at least one affiliation string was
    # present; otherwise stay inconclusive (None) and fall through to the
    # HTML/PDF tiers rather than excluding the paper.
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


# Tier 5 (PDF) helpers — see verify_via_pdf below. Last resort: only reached
# if openalex/crossref/publisher_html/ieee_browser were all inconclusive.

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
    """Find the OA PDF via Unpaywall, download it (capped ~10MB), read the
    first two pages (affiliations live on page 1) and look for Al-Baha."""
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


# Tier 4: a real (headless) browser render for JS-only publishers. IEEE
# Xplore's document page is an Angular SPA -- a plain `requests.get()` (used
# by Tier 3 below) gets back a bot-challenge stub (HTTP 202, ~2KB, no author
# data at all; verified empirically against a live DOI). A real browser
# executing the page's JS is not detected the same way and gets the full
# page -- and the author affiliations are sitting there as a clean JSON
# array (`"authors":[{"name":...,"affiliation":[...]}]`) that IEEE's own UI
# reads to populate the on-hover author tooltip. So no hover/click
# simulation is needed: load the page, pull that JSON straight out of the
# rendered HTML.
#
# Scoped to IEEE DOI prefixes only -- other blocked-by-scraping publishers
# would need their own JSON/selector research before being added here.

IEEE_DOI_PREFIXES = ('10.1109/', '10.1049/')  # IEEE + IET (co-published via Xplore)

_selenium_driver = None  # lazy singleton -- one browser reused across the whole run


def _get_selenium_driver():
    """Start (once) a headless Chrome for the IEEE render tier. Returns None
    if selenium/Chrome isn't available -- callers must degrade gracefully,
    same as the pypdf/bs4 lazy imports elsewhere in this file."""
    global _selenium_driver
    if _selenium_driver is not None:
        return _selenium_driver
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        return None

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_argument('--window-size=1400,1000')
    opts.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
    try:
        _selenium_driver = webdriver.Chrome(options=opts)
    except Exception:
        return None

    import atexit
    atexit.register(lambda: _selenium_driver.quit())
    return _selenium_driver


def _extract_ieee_authors_json(html: str) -> Optional[list]:
    """Pull the `"authors":[{"name":...,"affiliation":[...]}, ...]` array out
    of IEEE's rendered page HTML via bracket-matching (it's embedded inside a
    larger JS object literal, not standalone JSON, so a full-document
    json.loads() won't work)."""
    idx = html.find('"authors":[{"name"')
    if idx < 0:
        return None
    start = html.find('[', idx)
    depth, end = 0, None
    for i in range(start, min(start + 20000, len(html))):
        if html[i] == '[':
            depth += 1
        elif html[i] == ']':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(html[start:end])
    except json.JSONDecodeError:
        return None


def verify_via_ieee_browser(doi: str) -> tuple[Optional[bool], dict[str, Any]]:
    """Render the IEEE Xplore page in a real headless browser and inspect the
    embedded per-author affiliation JSON. Same (verified, evidence) contract
    as the other tiers."""
    if not doi:
        return None, {'reason': 'no_doi'}
    clean_doi = doi.strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if clean_doi.lower().startswith(prefix):
            clean_doi = clean_doi[len(prefix):]
    if not clean_doi.lower().startswith(IEEE_DOI_PREFIXES):
        return None, {'reason': 'not_ieee_doi'}

    driver = _get_selenium_driver()
    if driver is None:
        return None, {'reason': 'selenium_unavailable'}

    try:
        from selenium.webdriver.support.ui import WebDriverWait
        driver.get(f'https://doi.org/{clean_doi}')
        try:
            WebDriverWait(driver, 15).until(
                lambda d: '"authors":[' in d.page_source
                or 'ieeexplore.ieee.org' not in d.current_url
            )
        except Exception:
            pass  # fall through -- we still try to read whatever loaded
        current_url = driver.current_url
        if 'ieeexplore.ieee.org' not in current_url:
            return None, {'reason': 'not_ieee_after_redirect', 'url': current_url[:200]}
        html = driver.page_source
    except Exception as e:
        return None, {'reason': 'browser_failed', 'detail': str(e)[:200]}

    authors = _extract_ieee_authors_json(html)
    if authors is None:
        return None, {'reason': 'no_authors_json_found', 'url': current_url[:200]}

    affiliations_seen = 0
    for a in authors:
        name = a.get('name', '') if isinstance(a, dict) else ''
        for aff in (a.get('affiliation') or []) if isinstance(a, dict) else []:
            if aff:
                affiliations_seen += 1
            match = detect_albaha_in_text(aff)
            if match:
                return True, {
                    'tier': 'ieee_browser',
                    'matched_author': name,
                    'matched_affiliation': aff,
                    'matched_substring': match,
                    'url': current_url[:300],
                }

    if affiliations_seen == 0:
        return None, {'tier': 'ieee_browser', 'reason': 'no_affiliations_in_json',
                       'url': current_url[:200]}
    return False, {
        'tier': 'ieee_browser',
        'reason': 'no_albaha_in_affiliations',
        'authors_count': len(authors),
        'affiliations_seen': affiliations_seen,
        'url': current_url[:300],
    }


# Tier 3: the publisher's HTML landing page. Unlike the PDF tier it sees the
# page's structured metadata — citation/Dublin Core meta tags, JSON-LD, and
# publisher-specific affiliation selectors — falling back to a full-text
# scan. This also catches affiliations hidden behind a "show more authors"
# toggle: they're in the raw HTML, just not rendered until JS unhides them.

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
    """Resolve the DOI to the publisher's page and check affiliation info,
    most reliable strategy first (meta tags, JSON-LD, selectors, full text)."""
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

    # Strategy 1: citation / Dublin Core meta tags (highest confidence).
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

    # Strategy 2: JSON-LD structured data.
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

    # Strategy 3: publisher-specific CSS selectors.
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

    # Strategy 4: full HTML text search (last resort). Strip nav/ads/footers
    # first, and crucially drop the references / bibliography / acknowledgement
    # / cited-by blocks — a paper that merely cites or thanks an Al-Baha author
    # would otherwise be mislabelled as Al-Baha.
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

    # An authoritative FALSE requires the page to actually CARRY structured
    # affiliation data (meta tags / affiliation elements) that simply didn't
    # include Al-Baha. A bare full-text miss is inconclusive (the page may render
    # affiliations via JS, or our parser missed them) -> None, so it never
    # excludes a paper on absence alone.
    has_structured = False
    for meta in soup.find_all('meta'):
        nm = (meta.get('name', '') or meta.get('property', '') or '').lower()
        if any(p in nm for p in meta_patterns) and (meta.get('content') or '').strip():
            has_structured = True
            break
    if not has_structured:
        try:
            has_structured = bool(soup.select_one(
                '[class*="affiliation"], [id*="aff"], .aff, .NLM_aff'))
        except Exception:
            has_structured = False

    if has_structured:
        return False, {
            'tier': 'publisher_html',
            'reason': 'structured_affiliation_no_albaha',
            'url': final_url[:300],
            'html_length': len(html),
        }
    return None, {
        'tier': 'publisher_html',
        'reason': 'no_structured_affiliation_data',
        'url': final_url[:300],
        'html_length': len(html),
    }


TIERS = {
    'openalex':        verify_via_openalex,
    'crossref':        verify_via_crossref,
    'publisher_html':  verify_via_publisher_html,
    'ieee_browser':    verify_via_ieee_browser,
    'pdf':             verify_via_pdf,
}
# AUTHORITY MODEL (accuracy > speed — this is a batch verifier, not a fast one):
# the OFFICIAL record is decisive; mediated databases only support it.
#   Authoritative (may return a final TRUE or FALSE): the publisher's own
#     landing page, IEEE's rendered page, and the article PDF.
#   Supporting  (evidence only — a TRUE is accepted at LOWER confidence, a
#     negative is NEVER a final FALSE): OpenAlex, Crossref. They frequently ship
#     empty/partial affiliations, so their "no Al-Baha" means "not enough data",
#     not "not Al-Baha". Only an official source may exclude a paper.
AUTHORITATIVE_TIERS = ['publisher_html', 'ieee_browser', 'pdf']
SUPPORTING_TIERS    = ['openalex', 'crossref']
# Back-compat for --tier <name> (single forced tier is run standalone).
DEFAULT_TIER_ORDER = AUTHORITATIVE_TIERS + SUPPORTING_TIERS

TIER_DELAYS = {
    'openalex':       OPENALEX_DELAY,
    'crossref':       CROSSREF_DELAY,
    'publisher_html': 0.2,  # be polite — many publishers rate-limit aggressively
    'ieee_browser':   1.0,  # be extra polite — real page loads, not an API
    'pdf':            UNPAYWALL_DELAY,
}


def _run_tier(tier_name: str, doi: Optional[str]):
    """Run one tier, never letting an exception abort the pipeline."""
    time.sleep(TIER_DELAYS[tier_name])
    try:
        v, ev = TIERS[tier_name](doi or '')
    except Exception as e:
        v, ev = None, {'reason': 'tier_exception', 'detail': str(e)[:200]}
    if isinstance(ev, dict):
        ev.setdefault('tier', tier_name)
    return v, ev


def _result(verified, source, confidence, detail, trail,
            decision_basis=None, official=False):
    """Assemble the DB-ready dict, keeping the VERDICT separate from the
    EVIDENCE. Details always carry: `decision_basis` (the tier the final verdict
    rests on) + `official_source` (was it an authoritative source?) so the
    decision is self-explaining; `confidence` (authoritative/supporting/none);
    and the full `evidence_trail` (every source consulted, positive OR negative)
    so an audit never needs to re-run the paper."""
    return {
        'verified': verified,
        'verification_source': source,
        'details': {
            **(detail or {}),
            'decision_basis':  decision_basis or (source if verified is not None else None),
            'official_source': official,
            'confidence':      confidence,
            'verifier_version': VERIFIER_VERSION,
            'evidence_trail':  trail,
        },
    }


_TITLE_STOP = {'a', 'an', 'the', 'of', 'for', 'and', 'in', 'on', 'to', 'with', 'via', 'using'}


def _norm_title_tokens(t: Optional[str]) -> set:
    if not t:
        return set()
    t = unicodedata.normalize('NFKD', t)
    t = ''.join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    return {w for w in t.split() if w and w not in _TITLE_STOP}


def _title_match(our_tokens: set, cand_title: Optional[str],
                 our_year, cand_year) -> bool:
    b = _norm_title_tokens(cand_title)
    if not our_tokens or not b:
        return False
    jac = len(our_tokens & b) / len(our_tokens | b)
    year_ok = (our_year is None or cand_year is None
               or abs(int(cand_year) - int(our_year)) <= 1)
    return jac >= 0.9 and year_ok


def resolve_doi_by_title(title: Optional[str], year=None, authors_hint: Optional[str] = None):
    """P1: recover a DOI for a no-DOI paper by TITLE search — Crossref first,
    then OpenAlex. Conservative: accepts only a near-exact title match (token
    Jaccard >= 0.9) within +/-1 year, so we never attach a different work's DOI
    (the same trap that caused past contamination). Google Scholar is
    deliberately NOT used (no official API, unstable/limited). Returns
    (doi, via) or (None, None)."""
    toks = _norm_title_tokens(title)
    if not toks:
        return None, None
    hint = _norm_title_tokens(authors_hint) if authors_hint else None

    def _author_ok(family_names):
        # When we have our own author list, require at least one candidate
        # author family name to appear in it — guards against similar titles by
        # DIFFERENT authors slipping past the title+year check. When we have no
        # author data (hint is None) or the candidate lists none, don't block.
        if hint is None or not family_names:
            return True
        for fam in family_names:
            f = re.sub(r'[^a-z]', '', (fam or '').lower())
            if f and f in hint:
                return True
        return False

    # Crossref (free, no budget) first.
    try:
        r = requests.get('https://api.crossref.org/works',
                         params={'query.bibliographic': (title or '')[:300], 'rows': 5,
                                 'select': 'DOI,title,issued,author'},
                         headers={'User-Agent': USER_AGENT}, timeout=API_TIMEOUT)
        if r.status_code == 200:
            for it in (((r.json() or {}).get('message') or {}).get('items') or []):
                ct = (it.get('title') or [''])[0]
                try:
                    cy = (it.get('issued', {}).get('date-parts') or [[None]])[0][0]
                except (IndexError, TypeError):
                    cy = None
                fams = [a.get('family', '') for a in (it.get('author') or [])]
                if it.get('DOI') and _title_match(toks, ct, year, cy) and _author_ok(fams):
                    return it['DOI'], 'crossref-title'
    except (requests.RequestException, ValueError):
        pass
    time.sleep(CROSSREF_DELAY)
    # OpenAlex fallback.
    try:
        r = requests.get('https://api.openalex.org/works',
                         params={'search': (title or '')[:300], 'per-page': 5,
                                 'select': 'doi,title,publication_year,authorships'},
                         headers={'User-Agent': USER_AGENT}, timeout=API_TIMEOUT)
        if r.status_code == 200:
            for w in ((r.json() or {}).get('results') or []):
                names = [((a.get('author') or {}).get('display_name') or '')
                         for a in (w.get('authorships') or [])]
                fams = [n.split()[-1] for n in names if n.strip()]
                if (w.get('doi') and _title_match(toks, w.get('title'),
                                                  year, w.get('publication_year'))
                        and _author_ok(fams)):
                    return (w['doi'] or '').replace('https://doi.org/', ''), 'openalex-title'
    except (requests.RequestException, ValueError):
        pass
    time.sleep(OPENALEX_DELAY)
    return None, None


def verify_paper(
    paper_id: int,
    doi: Optional[str],
    source: str,
    tiers_to_run: Optional[list[str]] = None,
    mode: str = 'strict',
) -> dict[str, Any]:
    """Authority-weighted verification.

    Phase A — AUTHORITATIVE (publisher page / IEEE render / PDF): decisive.
      first TRUE  -> TRUE ; a structured-but-no-Al-Baha -> FALSE.
    Phase B — SUPPORTING (OpenAlex / Crossref): consulted only when no official
      source produced a verdict. A supporting Al-Baha match -> TRUE at LOWER
      confidence; a supporting negative is EVIDENCE only, never a final FALSE
      (a mediated DB being incomplete must never exclude a paper) -> stays NULL.

    A single forced tier (--tier X) is run standalone (native verdict) for
    debugging one source.
    """
    if tiers_to_run and len(tiers_to_run) == 1:
        t = tiers_to_run[0]
        v, ev = _run_tier(t, doi)
        conf = 'authoritative' if t in AUTHORITATIVE_TIERS else 'supporting'
        return _result(v, t if v is not None else 'pending-review', conf, ev, [ev],
                       decision_basis=t, official=(t in AUTHORITATIVE_TIERS))

    trail: list = []

    # Authoritative sources for THIS paper. IEEE render only for IEEE DOIs
    # (skip it entirely otherwise — it can't help a non-IEEE paper). Publisher
    # HTML + PDF apply to everything.
    clean_doi = (doi or '').strip().lower()
    for _pfx in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if clean_doi.startswith(_pfx):
            clean_doi = clean_doi[len(_pfx):]
    auth_tiers = ['publisher_html']
    if clean_doi.startswith(IEEE_DOI_PREFIXES):
        auth_tiers.append('ieee_browser')
    auth_tiers.append('pdf')

    # --- Phase A: authoritative. STRICT (default) runs EVERY official source and
    # keeps ALL evidence (Publisher+PDF double-confirm recorded, fully auditable
    # without re-processing). PERFORMANCE stops at the first decisive official
    # answer (tiny coverage loss for a big speed win on large batches). The
    # verdict rests on the first positive (or, absent any, the first structured
    # negative).
    auth_positive: list = []
    auth_negative: list = []
    for t in auth_tiers:
        v, ev = _run_tier(t, doi)
        trail.append(ev)
        if v is True:
            auth_positive.append((t, ev))
            if mode == 'performance':
                break
        elif v is False:                    # official record, structured data, no Al-Baha
            auth_negative.append((t, ev))
            if mode == 'performance':
                break
    if auth_positive:
        t0, ev0 = auth_positive[0]
        return _result(True, t0, 'authoritative', ev0, trail, decision_basis=t0, official=True)
    if auth_negative:
        t0, ev0 = auth_negative[0]
        return _result(False, t0, 'authoritative', ev0, trail, decision_basis=t0, official=True)

    # --- Phase B: supporting — run all for a full trail; evidence only.
    support_positive: list = []
    support_negatives: list = []
    for t in SUPPORTING_TIERS:
        v, ev = _run_tier(t, doi)
        trail.append(ev)
        if v is True:
            support_positive.append((t, ev))
        elif v is False:
            support_negatives.append(ev)    # EVIDENCE-, never a verdict on its own
    if support_positive:
        t0, ev0 = support_positive[0]
        return _result(True, t0, 'supporting', ev0, trail, decision_basis=t0, official=False)

    # No official data + no supporting positive -> NULL (pending-review),
    # carrying the supporting negatives so a reviewer sees why it's unresolved.
    return _result(None, 'pending-review', 'none',
                   {'reason': 'no_authoritative_data',
                    'supporting_negatives': support_negatives[:4]}, trail,
                   decision_basis=None, official=False)


def default_verification_years() -> list[int]:
    """Dashboard-scope years, computed dynamically (last year through next).

    It used to be hard-coded to [2025, 2026], which silently skipped papers
    in later years — an incremental scrape can pull next-year papers (Scholar
    lists in-press 2027 items today). Overridable via --years.
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
        # Version-aware resume: pick papers not yet verified (NULL) AND papers
        # verified by an OLDER algorithm version. This makes a full migration to
        # a new VERIFIER_VERSION resumable — a killed/re-started run just skips
        # what the current version already did, and re-verifies stale results.
        where.append(
            '(rp."AffiliationVerified" IS NULL '
            'OR COALESCE(rp."VerificationDetails"->>%s, %s) <> %s)'
        )
        params.extend(['verifier_version', '', VERIFIER_VERSION])

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
            rp."VerificationSource",
            rp."PubYear",
            rp."RawData_Log"->>'authors' AS authors_raw
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
        'resolved_doi': 0,
        'unresolvable_no_doi': 0,
    }

    for idx, paper in enumerate(papers, 1):
        title_preview = (paper['Title'] or '')[:60]
        doi = paper['DOI']

        # P1: no stored DOI -> recover one by TITLE (Crossref -> OpenAlex) so
        # book chapters / old conferences / local journals get verified too,
        # instead of being skipped. Only a near-exact title match is accepted.
        if not doi or doi.strip() == '':
            doi, via = resolve_doi_by_title(
                paper['Title'], paper.get('PubYear'), paper.get('authors_raw'))
            if not doi:
                log.warning(f'[{idx}/{len(papers)}] Paper #{paper["PaperID"]} — '
                            f'no DOI, none found by title. Title: {title_preview}')
                stats['unresolvable_no_doi'] += 1
                continue
            stats['resolved_doi'] += 1
            log.info(f'[{idx}/{len(papers)}] Paper #{paper["PaperID"]} — DOI recovered '
                     f'via {via}: {doi[:50]}')
            # Persist the recovered DOI + its provenance (DoiResolvedBy/At) so we
            # always know this DOI was inferred, not original. Guarded; a
            # duplicate-DOI collision just rolls back (still verify in-memory).
            if args.apply:
                try:
                    with conn.cursor() as c:
                        c.execute('UPDATE "ResearchPaper" SET "DOI"=%s, '
                                  '"DoiResolvedBy"=%s, "DoiResolvedAt"=NOW() '
                                  'WHERE "PaperID"=%s AND ("DOI" IS NULL OR "DOI"=\'\')',
                                  [doi, via, paper['PaperID']])
                    conn.commit()
                except Exception:
                    conn.rollback()
        else:
            log.info(f'[{idx}/{len(papers)}] Paper #{paper["PaperID"]} (DOI={doi[:50]})')

        result = verify_paper(
            paper_id=paper['PaperID'],
            doi=doi,
            source=paper['Source'],
            tiers_to_run=tiers_to_run,
            mode=getattr(args, 'mode', 'strict'),
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
    print(f'  Processed:            {stats["processed"]}')
    print(f'  Al-Baha verified:     {stats["verified_yes"]}')
    print(f'  NOT Al-Baha:          {stats["verified_no"]}')
    print(f'  Pending (retry):      {stats["pending"]}')
    print(f'  DOI recovered by title:{stats["resolved_doi"]}')
    print(f'  No DOI (unresolvable): {stats["unresolvable_no_doi"]}')
    print(f'  Mode:                 {"APPLY (writes to DB)" if args.apply else "DRY-RUN (no writes)"}')
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
        epilog='Modes: --dry-run / --apply / --report. See flags below for source, tier and scope.',
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='Walk through, do not write to DB')
    g.add_argument('--apply',   action='store_true', help='Write verification results to DB')
    g.add_argument('--report',  action='store_true', help='Print current verification stats only')

    parser.add_argument('--source', choices=['Scholar', 'Scopus', 'OpenAlex', 'Manual', 'Manual_EBSCO'],
                        help='Restrict to one source (default: all)')
    parser.add_argument('--tier', choices=list(TIERS.keys()),
                        help='Run a single tier only (default: all in order)')
    parser.add_argument('--mode', choices=['strict', 'performance'], default='strict',
                        help='strict (default): run every authoritative source and '
                             'keep all evidence. performance: stop at the first '
                             'decisive official answer (faster, tiny coverage loss).')
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
