"""Deterministic, pure normalization utilities for the DOI resolver.

No I/O. No DB. No network. No global mutable state. Every function is a
pure function of its inputs -- same input always produces the same output.

DESIGN NOTE ON STEMMING
-----------------------
Title/venue tokenization here removes a short, fixed stopword list only
(articles, prepositions, "based"/"using"/"via"...). It never stems or
merges related-but-distinct scientific terms ("network" vs "networked",
"sensor" vs "observation") -- Phase 0/1 of this pipeline's audit found a
real case (PaperID 7630) where exactly that kind of word difference is the
only signal separating "same paper, revised title" from "different paper,
similar topic". Aggressive stemming would erase the one signal that lets a
human reviewer (or a later, stronger signal) tell the two apart.
"""
from __future__ import annotations

import re
import unicodedata
from typing import FrozenSet, Optional

# -----------------------------------------------------------------------------
# Title normalization
# -----------------------------------------------------------------------------

# Conservative stopword list: short structural/connective English words
# only. Documented here because Phase 2 explicitly forbids silent/arbitrary
# choices -- this exact set is what backend/backfill_missing_dois.py already
# used in production (_STOP), kept identical for continuity/auditability.
TITLE_STOPWORDS: FrozenSet[str] = frozenset({
    'a', 'an', 'the', 'of', 'for', 'and', 'in', 'on', 'to', 'with', 'via',
    'using', 'based', 'from', 'by', 'is', 'are', 'as', 'at', 'into', 'over',
})

_WS_RE = re.compile(r'\s+')
_APOSTROPHE_RE = re.compile(r"[‘’ʼ`´]")  # curly/backtick/acute -> '
_HYPHEN_RE = re.compile(r'[‐‑‒–—―−]')  # dash variants -> '-'
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s'-]")


def normalize_title(text: Optional[str]) -> str:
    """Lowercase, Unicode-normalize (NFKD), strip diacritics, normalize
    apostrophe/hyphen variants, drop remaining punctuation, collapse
    whitespace. Handles None safely (-> ''). Does NOT remove stopwords or
    stem -- kept human-legible for provenance/debug display. Stopword
    removal only happens in title_tokens(), for the Jaccard signal.
    """
    if not text:
        return ''
    t = unicodedata.normalize('NFKD', text)
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = _APOSTROPHE_RE.sub("'", t)
    t = _HYPHEN_RE.sub('-', t)
    t = _NON_ALNUM_RE.sub(' ', t)
    t = _WS_RE.sub(' ', t).strip()
    return t


def title_tokens(text: Optional[str]) -> FrozenSet[str]:
    """Token set for title_token_jaccard(): normalize_title() output, split
    on whitespace/hyphen, stopwords and single-character tokens dropped.
    No stemming -- see module docstring."""
    norm = normalize_title(text)
    if not norm:
        return frozenset()
    return frozenset(
        w for w in norm.replace('-', ' ').split()
        if w and w not in TITLE_STOPWORDS and len(w) > 1
    )


def title_token_jaccard(a: Optional[str], b: Optional[str]) -> float:
    """|tokens(a) ∩ tokens(b)| / |tokens(a) ∪ tokens(b)|.

    Chosen over SequenceMatcher as the PRIMARY title metric per the Phase 0
    audit finding: SequenceMatcher's character-level ratio scored the known
    false-candidate pair for PaperID 7618 at 0.779 (above the old 0.72
    threshold); token-Jaccard on the same pair scores 0.5, because the two
    titles share few complete words despite overlapping characters.
    SequenceMatcher is still exposed for CALLERS that want it as a
    secondary/diagnostic signal, but this function is the one the resolver
    scores against.

    Returns 0.0 (never None) when either side has no tokens -- an empty
    title must never register as a "match" by division-by-zero or
    vacuous-truth accident.
    """
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# -----------------------------------------------------------------------------
# Author normalization
# -----------------------------------------------------------------------------

_NAME_TOKEN_RE = re.compile(r"[A-Za-zÀ-ɏ]{2,}")


def normalize_author(text: Optional[str]) -> str:
    """Latin-script author-name normalization: NFKD diacritics stripped,
    lowercased, whitespace collapsed.

    Deliberately Latin-only. Comparing Crossref/OpenAlex Latin author names
    against Users.FullName_Ar (Arabic) is explicitly out of scope for this
    signal -- Phase 0/1 found a real row (PaperID 5645) where
    Authors.AuthorNameRaw was empty, leaving zero comparable Latin author
    evidence even though an Arabic name existed in Users.FullName_Ar. That
    case is handled by author_signal() in resolver.py returning
    (None, None) -- "missing evidence", not "no match".
    """
    if not text:
        return ''
    t = unicodedata.normalize('NFKD', text)
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return _WS_RE.sub(' ', t.lower()).strip()


def author_surname(name: Optional[str]) -> str:
    """Crude but conservative: the last Latin alphabetic token (>=2 chars)
    in the normalized name, e.g. 'Saad Alqithami' -> 'alqithami'. Surnames
    are used as the comparison unit (not full names) because given-name
    transliteration varies far more than surnames across Crossref/OpenAlex/
    our own scraped data (e.g. 'Mohammed' vs 'Mohamed' vs 'Muhammad').
    Returns '' if no usable token is found.
    """
    toks = _NAME_TOKEN_RE.findall(normalize_author(name))
    return toks[-1] if toks else ''


# -----------------------------------------------------------------------------
# DOI normalization
# -----------------------------------------------------------------------------

_DOI_WRAPPER_RE = re.compile(
    r'^\s*(?:https?://)?(?:dx\.)?doi\.org/|^\s*doi:\s*', re.IGNORECASE)
_DOI_SHAPE_RE = re.compile(r'^10\.\d{4,9}/\S+$')
_TRAILING_PUNCT_RE = re.compile(r'''[.,;:)\]}'"]+$''')


def normalize_doi(text: Optional[str]) -> Optional[str]:
    """Canonicalize a DOI string for comparison/duplicate-detection.

    Strips (in order): surrounding whitespace/quotes, a leading
    https://doi.org/ | http://doi.org/ | https://dx.doi.org/ | doi: wrapper
    (case-insensitive), trailing punctuation (. , ; : ) ] } ' "), then
    lowercases. DOIs are case-insensitive by the DOI Foundation's own
    specification, so lowercasing here does not change the DOI's identity
    -- it mirrors the DB's own `uq_paper_doi_normalized` unique index
    (`lower(trim("DOI"))`).

    Returns None -- never a guessed/repaired value -- if the result does
    not match the canonical `10.<4-9 digits>/<suffix>` shape. This
    satisfies the Phase 2 requirement: "NEVER invent or repair a DOI by
    guessing." A None-returning input should be treated by callers as "not
    a usable DOI", not silently coerced into one.
    """
    if not text:
        return None
    t = text.strip().strip('"\'')
    t = _DOI_WRAPPER_RE.sub('', t)
    t = t.strip()
    t = _TRAILING_PUNCT_RE.sub('', t)
    t = t.lower()
    if not _DOI_SHAPE_RE.match(t):
        return None
    return t


def is_same_doi(a: Optional[str], b: Optional[str]) -> bool:
    """True only if both inputs normalize to the same non-None DOI."""
    na, nb = normalize_doi(a), normalize_doi(b)
    return na is not None and na == nb


# -----------------------------------------------------------------------------
# Venue normalization
# -----------------------------------------------------------------------------

def normalize_venue(text: Optional[str]) -> str:
    """Lowercase, diacritics-stripped, punctuation-stripped, whitespace-
    collapsed venue/journal name. Deliberately does not strip generic
    conference/publisher noise words ('proceedings of', 'international',
    ...) here -- that filtering is a fuzzy-matching concern handled by
    venue_similarity() in resolver.py, not a canonicalization concern.
    """
    if not text:
        return ''
    t = unicodedata.normalize('NFKD', text)
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = _NON_ALNUM_RE.sub(' ', t)
    return _WS_RE.sub(' ', t).strip()


# -----------------------------------------------------------------------------
# Pages normalization
# -----------------------------------------------------------------------------

_PAGE_DASH_RE = re.compile(r'[‐–—−-]+')
_PAGE_TRAIL_RE = re.compile(r'\.+$')


def normalize_pages(text: Optional[str]) -> str:
    """Normalize page-range formatting so '123-130', '123–130', and
    '123–130.' all compare equal. Returns '' for None/empty (missing
    pages must never be mistaken for a contradiction -- see
    bibliographic_score() in resolver.py, which treats '' as "no evidence",
    not "mismatch")."""
    if not text:
        return ''
    t = _PAGE_TRAIL_RE.sub('', text.strip())
    t = _PAGE_DASH_RE.sub('-', t)
    return _WS_RE.sub('', t)
