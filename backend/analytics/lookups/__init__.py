"""
Litrix lookup library - paper/journal metadata enrichment via
CrossRef, OpenAlex, and Scimago.

Public API:
    paper_by_doi(doi)         -> dict | None
    paper_by_title(title)     -> dict | None
    journal_by_issn(issns)    -> dict | None
    journal_by_name(name)     -> dict | None
    lookup(query)             -> dict
"""
from .core import (
    paper_by_doi,
    paper_by_title,
    journal_by_issn,
    journal_by_name,
    lookup,
)

__all__ = [
    "paper_by_doi",
    "paper_by_title",
    "journal_by_issn",
    "journal_by_name",
    "lookup",
]
