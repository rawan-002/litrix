"""
Diagnose papers reported as 'missing' by scrape_via_serpapi.py.

Reads missing_papers.csv and for each row, searches ResearchPaper
with a fuzzy match (lowercase + word-set similarity) to surface any
likely-existing paper that the exact NormalizedTitle match missed.

OUTPUT: missing_papers_diagnosed.csv with columns:
    scholar_id, missing_title, year,
    best_match_paper_id, best_match_title, similarity_score, verdict

VERDICT
    LIKELY_MATCH    similarity >= 0.85 — almost certainly the same paper,
                    just stored with slightly different punctuation/casing
    POSSIBLE_MATCH  similarity 0.65..0.85 — needs human review
    NOT_IN_DB       similarity < 0.65 — probably genuinely new paper
"""
import os, sys, csv, re
import django
from difflib import SequenceMatcher

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection


def normalise(s):
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', s).strip()


def fetch_db_titles():
    with connection.cursor() as cur:
        cur.execute('SELECT "PaperID", "Title", "NormalizedTitle", "PubYear" '
                    'FROM "ResearchPaper"')
        return cur.fetchall()


def best_match(missing_title, missing_year, all_papers):
    """Find the most similar paper in the DB."""
    norm_missing = normalise(missing_title)
    if not norm_missing:
        return None, None, 0.0

    best, best_score = None, 0.0
    for paper_id, title, ntitle, pyear in all_papers:
        # Compare via difflib (set-of-tokens similarity)
        cand = (ntitle or normalise(title)).strip()
        if not cand:
            continue
        # Quick early-out for obviously different lengths
        if abs(len(cand) - len(norm_missing)) > max(len(cand), len(norm_missing)) * 0.4:
            continue
        score = SequenceMatcher(None, norm_missing, cand).ratio()
        # Bonus if same year
        if missing_year and pyear and str(missing_year) == str(pyear):
            score += 0.05
        if score > best_score:
            best, best_score = (paper_id, title), score
    return (best[0] if best else None,
            best[1] if best else None,
            best_score)


def main():
    if not os.path.exists("missing_papers.csv"):
        print("missing_papers.csv not found. Run scrape_via_serpapi.py first.")
        return

    all_papers = fetch_db_titles()
    print(f"Loaded {len(all_papers)} papers from DB for matching.")

    out_rows = []
    with open("missing_papers.csv", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            title = row["title"]
            year  = row["year"]
            pid, ptitle, score = best_match(title, year, all_papers)
            if score >= 0.85:
                verdict = "LIKELY_MATCH"
            elif score >= 0.65:
                verdict = "POSSIBLE_MATCH"
            else:
                verdict = "NOT_IN_DB"
            out_rows.append({
                "scholar_id":          row["scholar_id"],
                "missing_title":       title,
                "year":                year,
                "best_match_paper_id": pid,
                "best_match_title":    ptitle,
                "similarity_score":    f"{score:.3f}",
                "verdict":             verdict,
            })

    # Sort by verdict so the LIKELY/POSSIBLE bubble up
    order = {"LIKELY_MATCH": 0, "POSSIBLE_MATCH": 1, "NOT_IN_DB": 2}
    out_rows.sort(key=lambda r: (order[r["verdict"]], -float(r["similarity_score"])))

    out_path = "missing_papers_diagnosed.csv"
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)

    # Summary
    counts = {"LIKELY_MATCH": 0, "POSSIBLE_MATCH": 0, "NOT_IN_DB": 0}
    for r in out_rows:
        counts[r["verdict"]] += 1

    print()
    print("=" * 60)
    print(f"Diagnosed {len(out_rows)} missing papers:")
    print(f"  LIKELY_MATCH   : {counts['LIKELY_MATCH']:3d}  (similarity ≥ 0.85)")
    print(f"  POSSIBLE_MATCH : {counts['POSSIBLE_MATCH']:3d}  (similarity 0.65–0.85)")
    print(f"  NOT_IN_DB      : {counts['NOT_IN_DB']:3d}  (similarity < 0.65)")
    print("=" * 60)
    print(f"\nReport: {out_path}")
    print()
    print("Sample LIKELY_MATCH rows:")
    for r in out_rows[:10]:
        if r["verdict"] != "LIKELY_MATCH": break
        print(f"  {r['similarity_score']}  {r['missing_title'][:60]!r}")
        print(f"             -> [{r['best_match_paper_id']}] "
              f"{(r['best_match_title'] or '')[:60]!r}")


if __name__ == "__main__":
    main()
