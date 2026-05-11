"""
DEPRECATED — replaceable with two lines of Django shell.

Removed Authors rows pointing at papers that aren't actually the
user's (mis-attributed by Scholar's fuzzy matcher). To do the same:

    $ python manage.py shell
    >>> from django.db import connection
    >>> with connection.cursor() as c:
    ...     c.execute(
    ...         'DELETE FROM "Authors" WHERE "UserID" = %s AND "PaperID" = ANY(%s)',
    ...         [user_id, [123, 456, 789]],
    ...     )
"""
raise SystemExit(
    'prune_user_papers.py is deprecated. Use Django shell — see the docstring.'
)
