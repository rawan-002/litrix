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

# Deliberately NOT registered: Frontiers "Research Topic" ebooks
# (10.3389/97[89]..., e.g. 10.3389/978-2-8325-6894-1 - an ISBN-shaped DOI,
# distinct from a regular Frontiers article DOI which always has a
# journal-code prefix instead, like 10.3389/fnins.2021.1234567). Investigated
# 2026-08-05 after PaperID 5526 ("Neuro-detection...", Najib Ben Aoun) showed
# up as VenueType='Book'. Crossref genuinely, consistently types this
# 10.3389/978-... shape as 'edited-book' with a real ISBN - Frontiers
# actually dual-publishes a Research Topic as both a web collection AND a
# print-on-demand ebook, so unlike Wiley's chapter-DOI convention this isn't
# a case of one authority disagreeing with itself; Crossref's answer is
# consistent and defensible. Only ONE row in the entire DB matched this shape
# - not enough evidence to justify a standing rule (that would be inferring a
# policy from a single example, the same overreach the Springer note above
# warns against). Revisit if this pattern shows up again at real volume.

PUBLISHER_DOI_RULES = [
    ('wiley-book-chapter', WILEY_BOOK_CHAPTER, BOOK_CHAPTER),
]
