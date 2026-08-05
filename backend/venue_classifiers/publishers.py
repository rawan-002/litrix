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

from .verdicts import BOOK_CHAPTER, JOURNAL

# Wiley book chapters: 10.1002/<13-digit ISBN>.ch<N>. This is Wiley's own
# chapter-DOI convention - exclusive to book chapters, never used for a
# journal article or a conference paper.
WILEY_BOOK_CHAPTER = re.compile(r'^10\.1002/97[0-9]{11}\.ch[0-9]+$', re.I)

# Frontiers "Research Topic" ebooks: 10.3389/<ISBN>, e.g.
# 10.3389/978-2-8325-6894-1. Frontiers compiles a journal's curated Research
# Topic (a themed set of ALREADY-published articles from that same journal,
# each with its own regular article DOI) into a downloadable PDF/EPUB and
# registers THAT compilation with Crossref as type='edited-book' - which is
# technically true metadata, but not what it means for OUR stats: the
# individual articles are already counted as Journal papers under their own
# authors, so counting the compilation too would both double-count content
# and misrepresent an editorial/curation credit as book authorship. A real
# Frontiers article DOI always has a journal-code prefix instead
# (10.3389/fnins.2021.1234567, never digits), so this shape is exclusive to
# the ebook compilation and safe to override back to Journal.
FRONTIERS_RESEARCH_TOPIC_EBOOK = re.compile(r'^10\.3389/97[89][\d-]+$', re.I)

# Deliberately NOT registered: Springer's 10.1007/978-... prefix. That shape
# is shared with legitimate LNCS/CCIS/AISC conference proceedings (the
# verifier's SERIAL_CONF name override already handles those), so a bare DOI
# regex can't tell book chapter from conference paper for Springer - it needs
# the Crossref/OpenAlex opinion path instead. Do not add it here without a
# second, independent signal to disambiguate.

PUBLISHER_DOI_RULES = [
    ('wiley-book-chapter', WILEY_BOOK_CHAPTER, BOOK_CHAPTER),
    ('frontiers-research-topic-ebook', FRONTIERS_RESEARCH_TOPIC_EBOOK, JOURNAL),
]
