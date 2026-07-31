"""One-command convenience wrapper — run BOTH lightweight update steps in order:

  1. scrapers/scholar_new_papers.py --apply   → fetch only genuinely NEW papers
     (incremental, cutoff-based; never re-touches existing papers)
  2. citations/refresh_hybrid.py --apply --all → refresh CitationsByYear for
     EVERY existing paper (no --year filter, so old papers' citation counts
     get updated too, not just 2025/2026); never touches paper metadata.

Neither step re-scrapes existing paper metadata — exactly the "easy path"
requested: keep old papers as-is, just refresh their citations + add new ones.

Usage (run from backend/):
    python refresh_and_new.py
    python refresh_and_new.py --new-budget 100 --serp-budget 50 --workers 2
    python refresh_and_new.py --skip-new            # citations refresh only
    python refresh_and_new.py --skip-citations       # new papers only
"""
import argparse
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
PY = sys.executable


def run(cmd, cwd):
    print(f"\n{'=' * 70}\n$ {' '.join(cmd)}\n{'=' * 70}\n", flush=True)
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        sys.exit(f"\n[FAILED] step exited with code {result.returncode}: {' '.join(cmd)}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-new", action="store_true",
                     help="Skip the new-papers discovery step.")
    ap.add_argument("--skip-citations", action="store_true",
                     help="Skip the citations-refresh step.")
    ap.add_argument("--new-budget", type=int, default=100,
                     help="SerpAPI call budget for new-paper discovery (default 100).")
    ap.add_argument("--serp-budget", type=int, default=50,
                     help="SerpAPI fallback budget for citation refresh "
                          "(default 50; pass 0 for OpenAlex-only, no SerpAPI spend).")
    ap.add_argument("--workers", type=int, default=2,
                     help="OpenAlex thread pool size for citation refresh "
                          "(default 2 — kept low; OpenAlex has been actively "
                          "rate-limiting this mailto/IP today at higher concurrency).")
    args = ap.parse_args()

    if not args.skip_new:
        run([PY, "scrapers/scholar_new_papers.py", "--apply",
             "--budget", str(args.new_budget)], cwd=BACKEND)

    if not args.skip_citations:
        cmd = [PY, "citations/refresh_hybrid.py", "--apply", "--all",
               "--workers", str(args.workers), "--write-citations-table"]
        if args.serp_budget:
            cmd += ["--serp-budget", str(args.serp_budget)]
        else:
            cmd += ["--no-serp"]
        run(cmd, cwd=BACKEND)

    print("\nDone — new papers fetched (if not skipped) and citations refreshed "
          "for the full existing corpus (if not skipped).")


if __name__ == "__main__":
    main()
