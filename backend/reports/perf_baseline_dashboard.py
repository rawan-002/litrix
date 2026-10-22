"""Baseline measurement for the Dashboard overview() endpoint, before the
safe query-merge optimization. Run with: python reports/perf_baseline_dashboard.py
Captures: query count, per-query DB time (via CaptureQueriesContext, works
regardless of DEBUG), and total backend wall-clock time. Read-only.
"""
import django, os, sys, io, time, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth import get_user_model
from analytics.views import overview

rf = RequestFactory()
User = get_user_model()
admin = User.objects.filter(user_type='Admin').first()

# warm the connection first so we measure steady-state, not cold-connect
with connection.cursor() as cur:
    cur.execute('SELECT 1'); cur.fetchone()

results = {}
for label, params in [('all_years_all_dept', {}), ('year_2025', {'year': '2025'})]:
    req = rf.get('/api/stats/overview/', params)
    req.user = admin
    with CaptureQueriesContext(connection) as ctx:
        t0 = time.perf_counter()
        resp = overview(req)
        t1 = time.perf_counter()
    total_ms = (t1 - t0) * 1000
    db_ms = sum(float(q['time']) for q in ctx.captured_queries) * 1000
    results[label] = {
        'status': resp.status_code,
        'query_count': len(ctx.captured_queries),
        'total_backend_ms': round(total_ms, 1),
        'total_db_ms': round(db_ms, 1),
        'non_db_ms': round(total_ms - db_ms, 1),
        'data': resp.data,
    }
    print(f"=== {label} ===")
    print(f"  status: {resp.status_code}")
    print(f"  query_count: {len(ctx.captured_queries)}")
    print(f"  total_backend_ms: {total_ms:.1f}")
    print(f"  total_db_ms (sum of query durations): {db_ms:.1f}")
    print(f"  non_db_ms (python/serialization): {total_ms - db_ms:.1f}")
    print()

with open('reports/perf_baseline_dashboard.json', 'w', encoding='utf-8') as f:
    json.dump({
        k: {kk: vv for kk, vv in v.items() if kk != 'data'} | {'data': v['data']}
        for k, v in results.items()
    }, f, indent=2, default=str, ensure_ascii=False)
print('Saved reports/perf_baseline_dashboard.json')
