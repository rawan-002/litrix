"""
Litrix lookup core - shared library for journal/paper metadata.

DESIGN NOTES
------------
1. Single shared requests.Session across all API calls -> connection
   reuse + lower TLS handshake overhead.
2. Configurable rate limit between requests (default 0.5s) so we don't
   trip CrossRef/OpenAlex rate limiters when running batches of 1000+.
3. Scimago CSV loaded lazily, once, into a module-level cache. Subsequent
   calls reuse it (~30K journals, fits in memory easily).
4. DuckDuckGo fallback is OPT-IN only. The base library never scrapes
   HTML - that's reserved for situations where the caller passes
   `try_web_fallback=True`.
5. Every external call is wrapped in try/except. A network failure for
   one paper never blows up the calling batch job.
6. Idempotent: same input -> same output (deterministic, except network).

USAGE
-----
    from analytics.lookups import paper_by_doi, journal_by_issn, lookup
    paper = paper_by_doi("10.1038/s41598-025-91911-2")
    journal = journal_by_issn(paper["issn"])
"""
from __future__ import annotations

import csv
import logging
import os
import re
import time
from difflib import SequenceMatcher
from typing import Iterable, List, Optional
from urllib.parse import quote_plus

import requests


logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Config (tunable from settings or environment)
# -----------------------------------------------------------------------------

REQUEST_TIMEOUT = 25
DOWNLOAD_TIMEOUT = 120
RATE_LIMIT_SECONDS = 0.5   # sleep between successive external API calls
USER_AGENT = ("Litrix/1.0 (research-analytics; mailto:admin@example.com) "
              "AppleWebKit/537.36")

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

# Scimago CSV cache path (the file is large; we keep it next to the module)
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SCIMAGO_FILE = os.path.join(BASE_DIR, "scimago_cache.csv")
SCIMAGO_URL  = "https://www.scimagojr.com/journalrank.php?out=xls"

# Match thresholds for fuzzy lookups
TITLE_MATCH_THRESHOLD     = 0.78  # Scimago name match
PAPER_TITLE_THRESHOLD     = 0.72  # CrossRef / OpenAlex title match

# Internal caches
_journals_by_issn: Optional[dict] = None
_journals_by_name: Optional[dict] = None
_session: Optional[requests.Session] = None
_last_request_at: float = 0.0


# -----------------------------------------------------------------------------
# Session + rate limiting
# -----------------------------------------------------------------------------

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def _rate_limit():
    """Block briefly so we stay polite to external APIs."""
    global _last_request_at
    delta = time.time() - _last_request_at
    if delta < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - delta)
    _last_request_at = time.time()


def _request_json(url: str, params=None) -> Optional[dict]:
    """GET + parse JSON. Returns None on any failure - never raises."""
    _rate_limit()
    try:
        r = _get_session().get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.debug("Network failure on %s: %s", url, exc)
        return None
    if r.status_code != 200:
        logger.debug("HTTP %s on %s", r.status_code, url)
        return None
    try:
        return r.json()
    except ValueError:
        logger.debug("Non-JSON response from %s", url)
        return None


# -----------------------------------------------------------------------------
# Normalizers
# -----------------------------------------------------------------------------

_DOI_PREFIX_RE = re.compile(r"^10\.\d{4,}/")
_HTML_TAGS_RE  = re.compile(r"<[^>]+>")
_WS_RE         = re.compile(r"\s+")
_NON_ALNUM_RE  = re.compile(r"[^\w\s]")
_DASH_VARIANTS = re.compile(r"[‐‑‒–—―−]")
_ISSN_RE       = re.compile(r"[^0-9Xx]")


def is_doi(text: str) -> bool:
    text = (text or "").strip()
    return "doi.org/" in text.lower() or bool(_DOI_PREFIX_RE.match(text))


def doi_value(text: str) -> str:
    text = (text or "").strip()
    if "doi.org/" in text.lower():
        text = text.split("doi.org/", 1)[-1]
    return text.strip().rstrip(".)]'")


def clean_text(text: str) -> str:
    text = _HTML_TAGS_RE.sub("", text or "")
    return _WS_RE.sub(" ", text).strip()


def clean_issn(text: str) -> str:
    return _ISSN_RE.sub("", text or "").upper()


def clean_name(text: str) -> str:
    text = (text or "").lower()
    text = _DASH_VARIANTS.sub("-", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def similarity(a: str, b: str) -> float:
    a, b = clean_name(a), clean_name(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# -----------------------------------------------------------------------------
# Scimago loader (cached)
# -----------------------------------------------------------------------------

def _download_scimago() -> bool:
    """Pull Scimago's annual ranking CSV. Returns True on success."""
    try:
        session = _get_session()
        session.get("https://www.scimagojr.com/", timeout=REQUEST_TIMEOUT)
        r = session.get(SCIMAGO_URL, timeout=DOWNLOAD_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Scimago download failed: %s", exc)
        return False
    if r.status_code != 200 or len(r.content) <= 100_000:
        logger.warning("Scimago response too small or non-200: %s, %s bytes",
                       r.status_code, len(r.content))
        return False
    with open(SCIMAGO_FILE, "wb") as f:
        f.write(r.content)
    return True


def _find_scimago_file() -> Optional[str]:
    """Locate a previously downloaded Scimago CSV in the lookups dir."""
    if os.path.exists(SCIMAGO_FILE) and os.path.getsize(SCIMAGO_FILE) > 100_000:
        return SCIMAGO_FILE
    for fname in os.listdir(BASE_DIR):
        low = fname.lower()
        if ("scimago" in low or low.startswith("scimagojr")) and low.endswith((".csv", ".xls")):
            path = os.path.join(BASE_DIR, fname)
            if os.path.getsize(path) > 100_000:
                if path != SCIMAGO_FILE:
                    with open(path, "rb") as src, open(SCIMAGO_FILE, "wb") as dst:
                        dst.write(src.read())
                return SCIMAGO_FILE
    return None


def _read_scimago():
    global _journals_by_issn, _journals_by_name
    if _journals_by_issn is not None:
        return _journals_by_issn, _journals_by_name

    if not _find_scimago_file() and not _download_scimago():
        logger.warning("Scimago CSV not available; journal enrichment disabled.")
        return None, None

    by_issn, by_name = {}, {}
    with open(SCIMAGO_FILE, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            row = {k: (v or "").strip().strip('"') for k, v in row.items() if k}
            title = row.get("Title", "")
            if title:
                by_name[clean_name(title)] = row
            for value in row.get("Issn", "").split(","):
                issn = clean_issn(value)
                if len(issn) == 8:
                    by_issn[issn] = row

    _journals_by_issn = by_issn
    _journals_by_name = by_name
    logger.info("Scimago loaded: %s by ISSN, %s by name",
                len(by_issn), len(by_name))
    return by_issn, by_name


def _parse_categories(text: str) -> List[dict]:
    items = []
    for part in (text or "").split(";"):
        part = part.strip().strip('"')
        if not part:
            continue
        m = re.match(r"^(.*?)\s*\(Q([1-4])\)\s*$", part)
        if m:
            items.append({"category": m.group(1).strip(), "quartile": f"Q{m.group(2)}"})
        else:
            items.append({"category": part, "quartile": "-"})
    return items


def _journal_row(row: dict) -> dict:
    sid = row.get("Sourceid", "")
    return {
        "journal_name":   row.get("Title", ""),
        "sourceid":       sid,
        "issns":          [clean_issn(x) for x in row.get("Issn", "").split(",") if clean_issn(x)],
        "publisher":      row.get("Publisher", ""),
        "country":        row.get("Country", ""),
        "region":         row.get("Region", ""),
        "type":           row.get("Type", ""),
        "open_access":    row.get("Open Access", ""),
        "sjr":            row.get("SJR", ""),
        "best_quartile":  row.get("SJR Best Quartile", ""),
        "h_index":        row.get("H index", ""),
        "coverage":       row.get("Coverage", ""),
        "areas":          row.get("Areas", ""),
        "categories":     _parse_categories(row.get("Categories", "")),
        "scimago_url":    f"https://www.scimagojr.com/journalsearch.php?q={sid}&tip=sid",
    }


# -----------------------------------------------------------------------------
# Public lookup functions
# -----------------------------------------------------------------------------

def journal_by_issn(issns: Optional[Iterable[str]]) -> Optional[dict]:
    by_issn, _ = _read_scimago()
    if by_issn is None:
        return None
    for value in issns or []:
        issn = clean_issn(value)
        if issn in by_issn:
            return _journal_row(by_issn[issn])
    return None


def journal_by_name(name: str) -> Optional[dict]:
    _, by_name = _read_scimago()
    if by_name is None or not name:
        return None

    key = clean_name(name)
    if key in by_name:
        return _journal_row(by_name[key])

    words = set(re.findall(r"\b\w{3,}\b", key))
    if not words:
        return None

    candidates = []
    for saved_name, row in by_name.items():
        saved_words = set(re.findall(r"\b\w{3,}\b", saved_name))
        if not saved_words:
            continue
        short, long_ = (words, saved_words) if len(words) <= len(saved_words) else (saved_words, words)
        overlap = len(short & long_) / len(short)
        if overlap >= 0.85:
            candidates.append((similarity(name, row.get("Title", "")), row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    if candidates and candidates[0][0] >= TITLE_MATCH_THRESHOLD:
        return _journal_row(candidates[0][1])
    return None


def _paper_from_crossref(doi: str) -> Optional[dict]:
    data = _request_json(f"https://api.crossref.org/works/{doi_value(doi)}")
    if not data:
        return None
    item = data.get("message", {})
    titles   = [clean_text(x) for x in item.get("title", []) if x]
    journals = [clean_text(x) for x in item.get("container-title", []) if x]
    date = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
    year = (date.get("date-parts") or [[None]])[0][0]
    return {
        "source":      "CrossRef",
        "paper_title": titles[0] if titles else "",
        "journal":     journals[0] if journals else "",
        "issn":        item.get("ISSN", []) or [],
        "publisher":   item.get("publisher", ""),
        "year":        year,
        "doi":         item.get("DOI", ""),
        "type":        item.get("type", ""),
    }


def _paper_from_openalex(doi: str) -> Optional[dict]:
    data = _request_json(f"https://api.openalex.org/works/doi:{doi_value(doi)}")
    if not data:
        return None
    location = data.get("primary_location") or {}
    source = location.get("source") or {}
    found_doi = data.get("doi", "") or ""
    issns = list(source.get("issn") or [])
    if source.get("issn_l") and source["issn_l"] not in issns:
        issns.insert(0, source["issn_l"])
    if "doi.org/" in found_doi.lower():
        found_doi = found_doi.split("doi.org/", 1)[-1]
    return {
        "source":      "OpenAlex",
        "paper_title": clean_text(data.get("title", "")),
        "journal":     source.get("display_name", ""),
        "issn":        issns,
        "publisher":   source.get("host_organization_name", ""),
        "year":        data.get("publication_year"),
        "doi":         found_doi,
        "type":        data.get("type", "") or source.get("type", ""),
    }


def paper_by_doi(doi: str) -> Optional[dict]:
    """Try CrossRef first, fallback to OpenAlex. Returns None if neither hits."""
    for fn in (_paper_from_crossref, _paper_from_openalex):
        paper = fn(doi)
        if paper and (paper.get("journal") or paper.get("issn")):
            return paper
    return None


def _best_crossref_title_match(title: str) -> Optional[dict]:
    data = _request_json("https://api.crossref.org/works",
                         {"query.title": title, "rows": 5})
    if not data:
        return None
    items = data.get("message", {}).get("items", [])
    best, best_score = None, 0.0
    for item in items:
        item_title = clean_text((item.get("title") or [""])[0])
        score = similarity(title, item_title)
        if score > best_score:
            best, best_score = item, score
    if not best or best_score < PAPER_TITLE_THRESHOLD:
        return None
    doi = best.get("DOI")
    if not doi:
        return None
    paper = paper_by_doi(doi)
    if paper:
        paper["doi_found_from_title"] = True
        paper["title_match_score"] = round(best_score, 3)
    return paper


def _best_openalex_title_match(title: str) -> Optional[dict]:
    data = _request_json(
        "https://api.openalex.org/works",
        {"search": title, "per-page": 5,
         "select": "doi,title,publication_year,primary_location"},
    )
    if not data:
        return None
    items = data.get("results", [])
    best, best_score = None, 0.0
    for item in items:
        item_title = clean_text(item.get("title", ""))
        score = similarity(title, item_title)
        if score > best_score:
            best, best_score = item, score
    if not best or best_score < PAPER_TITLE_THRESHOLD:
        return None
    doi = best.get("doi") or ""
    if not doi:
        return None
    paper = paper_by_doi(doi)
    if paper:
        paper["doi_found_from_title"] = True
        paper["title_match_score"] = round(best_score, 3)
    return paper


def paper_by_title(title: str) -> Optional[dict]:
    """Find a paper (and its DOI) from just a title via CrossRef -> OpenAlex."""
    if not title or len(title.strip()) < 8:
        return None
    for fn in (_best_crossref_title_match, _best_openalex_title_match):
        paper = fn(title)
        if paper:
            return paper
    return None


def lookup(query: str) -> dict:
    """High-level: accept DOI / journal name / paper title and return everything."""
    query = (query or "").strip()
    result = {"input": query, "paper": None, "scimago": None}
    if not query:
        return result

    if is_doi(query):
        paper = paper_by_doi(query)
        result["paper"] = paper
        if paper:
            result["scimago"] = (
                journal_by_issn(paper.get("issn"))
                or journal_by_name(paper.get("journal", ""))
            )
        return result

    journal = journal_by_name(query)
    if journal:
        result["scimago"] = journal
        return result

    paper = paper_by_title(query)
    result["paper"] = paper
    if paper:
        result["scimago"] = (
            journal_by_issn(paper.get("issn"))
            or journal_by_name(paper.get("journal", ""))
        )
    return result
