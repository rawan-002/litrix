"""Entry point: classify_from_doi(doi) -> one of BOOK / BOOK_CHAPTER /
JOURNAL / CONFERENCE / UNKNOWN.

Walks publishers.PUBLISHER_DOI_RULES in order and returns the first match.
UNKNOWN means "no publisher rule fired" - the caller (verify_venue_
authoritative.py) then falls back to its own stored-name/Crossref/OpenAlex/
DBLP opinion-gathering. This module never calls a network API; every rule
here is a plain DOI-shape check.
"""
from .publishers import PUBLISHER_DOI_RULES
from .verdicts import BOOK, BOOK_CHAPTER, CONFERENCE, JOURNAL, UNKNOWN

__all__ = ['BOOK', 'BOOK_CHAPTER', 'CONFERENCE', 'JOURNAL', 'UNKNOWN', 'classify_from_doi']


def classify_from_doi(doi):
    doi = (doi or '').strip()
    if not doi:
        return UNKNOWN
    for _name, pattern, verdict in PUBLISHER_DOI_RULES:
        if pattern.match(doi):
            return verdict
    return UNKNOWN
