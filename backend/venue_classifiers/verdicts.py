"""The four venue-type verdicts this package can hand back. Split into its
own module (rather than living in doi_patterns.py) so publishers.py can
import the constants without a circular import.
"""
BOOK = 'BOOK'
BOOK_CHAPTER = 'BOOK_CHAPTER'
JOURNAL = 'JOURNAL'
CONFERENCE = 'CONFERENCE'
UNKNOWN = 'UNKNOWN'
