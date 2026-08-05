"""Registry of publisher DOI-minting conventions that unambiguously identify
a venue type without needing Crossref/OpenAlex/DBLP to weigh in.

Each rule is (name, compiled_regex, verdict). Add new rules here, not as
inline `if` branches in verify_venue_authoritative.py or doi_patterns.py -
that's the entire point of this module existing as its own package.

A rule belongs here ONLY if the DOI prefix/shape is exclusive to one venue
type for that publisher. If a prefix is shared across venue types (the
disqualifying case), it does NOT belong here - see the Springer note below.
"""
import re

from .verdicts import BOOK_CHAPTER

# Wiley book chapters: 10.1002/<13-digit ISBN>.ch<N>. This is Wiley's own
# chapter-DOI convention - exclusive to book chapters, never used for a
# journal article or a conference paper.
WILEY_BOOK_CHAPTER = re.compile(r'^10\.1002/97[0-9]{11}\.ch[0-9]+$', re.I)

# Deliberately NOT registered: Springer's 10.1007/978-... prefix. That shape
# is shared with legitimate LNCS/CCIS/AISC conference proceedings (the
# verifier's SERIAL_CONF name override already handles those), so a bare DOI
# regex can't tell book chapter from conference paper for Springer - it needs
# the Crossref/OpenAlex opinion path instead. Do not add it here without a
# second, independent signal to disambiguate.

PUBLISHER_DOI_RULES = [
    ('wiley-book-chapter', WILEY_BOOK_CHAPTER, BOOK_CHAPTER),
]
