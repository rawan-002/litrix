"""Phase 2A -- offline, read-only benchmark runner.

Fixtures below are STATIC, copied from the already-verified read-only
Phase-2-design prototype run (real Crossref/OpenAlex responses fetched
during that earlier, separate read-only audit session; DB values read via
SELECT-only queries at that time). This script makes NO network calls and
NO database queries itself -- it only re-runs the pure resolver against
data that was already fetched and hand-verified.

Run:
    python backend/doi_pipeline/benchmark.py

WARNING (must not be glossed over): n=8. This is a small, manually
selected, hand-verified benchmark -- NOT a random sample of the 2031-paper
population, and NOT sufficient on its own to claim general production
precision. It demonstrates the resolver behaves correctly on every known
case this pipeline's audit has found so far, nothing broader.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

# Standalone-script bootstrap (same pattern already used throughout this
# repo's backend/*.py loose scripts) so `python backend/doi_pipeline/
# benchmark.py` works without requiring `doi_pipeline` to be installed/on
# PYTHONPATH as a package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from resolver import (  # noqa: E402
    AUTO_MATCH,
    REJECT,
    REVIEW,
    CandidateInput,
    PaperInput,
    resolve,
)


@dataclass(frozen=True)
class BenchmarkCase:
    paper_id: int
    label: str
    expected: str
    paper: PaperInput
    candidates: List[Optional[CandidateInput]]


CASES: List[BenchmarkCase] = [
    BenchmarkCase(
        paper_id=3013, label='KNOWN_WRONG_DOI (manual-entry misattribution)',
        expected=REJECT,
        paper=PaperInput(
            title='IoT Guard: An Efficient Intrusion Detection System for IoT '
                  'Using Optimized Feature Selection and Compact Traffic Representation',
            authors=(), year=2026,
            venue='Al-Baha University Journal of Basic and Applied Sciences',
        ),
        candidates=[CandidateInput(
            source='openalex(stored_doi_revalidation)', doi='10.25673/121007',
            title='An Efficient Intrusion Detection System for IoT Using Feature '
                  'Engineering and Machine Learning',
            authors=['Hassan Basima Kzar', 'Hassen Khawla Rashige', 'Faraj Sakna Jahiya'],
            year=2025, venue=None,
        )],
    ),
    BenchmarkCase(
        paper_id=5645, label='KNOWN_WRONG_DOI (manual-entry misattribution)',
        expected=REJECT,
        paper=PaperInput(
            title='Cooperative game theory-based approach for energy-aware '
                  'job scheduling in cloud',
            authors=(), year=2013,
            venue='International Journal Computers Their Applications',
        ),
        candidates=[CandidateInput(
            source='stored_doi_revalidation', doi='10.1109/coconet.2015.7411180',
            title='Cooperative game theoretic approach for job scheduling in cloud computing',
            authors=['Alaka Ananth', 'K. Chandrasekaran'], year=2015, venue='2015 CoCoNet',
        )],
    ),
    BenchmarkCase(
        paper_id=7618, label='NEAR_MISS -- source disagreement, correct match has no DOI',
        expected=REVIEW,
        paper=PaperInput(
            title='A Cooperative Game Theory-based Approach for Energy-Aware '
                  'Job Scheduling in Cloud.',
            authors=['Saad Alqithami'], year=2013,
            venue='International Journal Computers Their Applications',
        ),
        candidates=[
            CandidateInput(
                source='crossref', doi='10.1109/coconet.2015.7411180',
                title='Cooperative game theoretic approach for job scheduling in cloud computing',
                authors=['Alaka Ananth', 'K. Chandrasekaran'], year=2015, venue='2015 CoCoNet',
            ),
            CandidateInput(
                source='openalex', doi=None,
                title='A Cooperative Game Theory-based Approach for Energy-Aware '
                      'Job Scheduling in Cloud.',
                authors=['Mustafa Ibrahim Khaleel', 'Saad Alqithami', 'Michelle Zhu',
                         'Dunren Che', 'Wen-Chi Hou'],
                year=2013, venue=None,
            ),
        ],
    ),
    BenchmarkCase(
        paper_id=7620, label='SOURCE_DISAGREEMENT (one source noisy/irrelevant)',
        expected=REVIEW,
        paper=PaperInput(
            title='SPONTANEOUS NETWORKED ORGANIZATION',
            authors=['Saad Alqithami'], year=2012, venue='OpenSIUC',
        ),
        candidates=[
            CandidateInput(
                source='crossref', doi='10.4108/icst.collaboratecom.2012.250538',
                title='Spontaneous Organizations: Collaborative Computing Model '
                      'of a Networked Organization',
                authors=['Saad Alqithami'], year=2012, venue='CollaborateCom 2012',
            ),
            CandidateInput(
                source='openalex', doi='10.1073/pnas.0504136102',
                title='The human brain is intrinsically organized into dynamic, '
                      'anticorrelated functional networks',
                authors=['Michael Fox', 'Abraham Snyder'], year=2005, venue='PNAS',
            ),
        ],
    ),
    BenchmarkCase(
        paper_id=7615, label='SOURCE_AGREEMENT_BUT_WRONG_AUTHOR',
        expected=REJECT,
        paper=PaperInput(
            title='Intrusion detection in IPv6-enabled sensor networks',
            authors=['Mohammed Al Qurashi'], year=2020,
            venue='Bournemouth University Research Online',
        ),
        candidates=[
            CandidateInput(
                source='crossref', doi='10.1109/icc.2014.6883583',
                title='Policy and network-based intrusion detection system for '
                      'IPv6-enabled wireless sensor networks',
                authors=['Joao Amaral', 'Luis Oliveira', 'Joel Rodrigues',
                         'Guangjie Han', 'Lei Shu'],
                year=2014, venue='ICC 2014',
            ),
            CandidateInput(
                source='openalex', doi='10.1109/icc.2014.6883583',
                title='Policy and network-based intrusion detection system for '
                      'IPv6-enabled wireless sensor networks',
                authors=['Joao Amaral', 'Luis Oliveira', 'Joel Rodrigues',
                         'Guangjie Han', 'Lei Shu'],
                year=2014, venue='ICC 2014',
            ),
        ],
    ),
    BenchmarkCase(
        paper_id=7636, label='CLEAN_REJECT (title+author+source all disagree)',
        expected=REJECT,
        paper=PaperInput(
            title='A Survey of Security Issues for Data Sharing over Untrusted '
                  'Cloud Providers',
            authors=['Ebtesam Alomari'], year=2014, venue=None,
        ),
        candidates=[
            CandidateInput(
                source='crossref', doi='10.1109/cloudcom.2010.36',
                title='Trusted Data Sharing over Untrusted Cloud Storage Providers',
                authors=['Gansen Zhao', 'Chunming Rong', 'Jin Li', 'Feng Zhang', 'Yong Tang'],
                year=2010, venue=None,
            ),
            CandidateInput(
                source='openalex', doi='10.1186/1869-0238-4-5',
                title='An analysis of security issues for cloud computing',
                authors=['Keiko Hashizume'], year=2013, venue=None,
            ),
        ],
    ),
    BenchmarkCase(
        paper_id=7598, label='CLEAN_REJECT (low title score, no usable author evidence)',
        expected=REJECT,
        paper=PaperInput(
            title='Antimicrobial Gel Delivery Device for Oral Wound Sites',
            authors=['FK Alsheref', 'WM Elsayed', 'M Salah'], year=2026, venue=None,
        ),
        candidates=[
            CandidateInput(
                source='crossref', doi='10.1021/acsmaterialslett.4c00920.s001',
                title='Wearable eBandage with Antimicrobial Ionogel as an '
                      'Integrated Electroceutical Device for Accelerated Wound Healing',
                authors=[], year=None, venue=None,
            ),
            CandidateInput(
                source='openalex', doi='10.2147/ijn.s121956',
                title='The antimicrobial activity of nanoparticles: present '
                      'situation and prospects for the future',
                authors=['Linlin Wang'], year=None, venue=None,
            ),
        ],
    ),
    BenchmarkCase(
        paper_id=3015, label='KNOWN_CORRECT (BibTeX-confirmed ground truth)',
        expected=AUTO_MATCH,
        paper=PaperInput(
            title='The Feasibility of Using Random Administrative Samples (The '
                  'French Experiment) in Population Censuses in African Countries',
            authors=(), year=2023,
            venue='International Journal of Current Science Research and Review',
        ),
        candidates=[CandidateInput(
            source='stored_doi_revalidation', doi='10.47191/ijcsrr/V6-i2-93',
            title='The Feasibility of Using Random Administrative Samples (The '
                  'French Experiment) in Population Censuses in African Countries',
            authors=['Elkhatim Abuelysar Elmobarak'], year=2023, venue=None,
        )],
    ),
]


def run() -> tuple[list[dict], dict]:
    rows = []
    for case in CASES:
        result = resolve(case.paper, case.candidates, known_dois={})
        rows.append({
            'paper_id': case.paper_id,
            'label': case.label,
            'expected': case.expected,
            'predicted': result.decision,
            'pass': result.decision == case.expected,
            'score': result.score,
            'reasons': result.reasons,
            'source_agreement': result.source_agreement,
        })

    counts = {AUTO_MATCH: 0, REVIEW: 0, REJECT: 0}
    tp = fp = tn = fn = 0
    for r in rows:
        counts[r['predicted']] += 1
        predicted_positive = r['predicted'] == AUTO_MATCH
        actual_positive = r['expected'] == AUTO_MATCH
        if predicted_positive and actual_positive:
            tp += 1
        elif predicted_positive and not actual_positive:
            fp += 1
        elif not predicted_positive and not actual_positive:
            tn += 1
        else:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    summary = {
        'n': len(rows),
        'auto_match_count': counts[AUTO_MATCH],
        'review_count': counts[REVIEW],
        'reject_count': counts[REJECT],
        'true_positive': tp, 'false_positive': fp,
        'true_negative': tn, 'false_negative': fn,
        'precision': precision, 'recall': recall,
        'all_pass': all(r['pass'] for r in rows),
    }
    return rows, summary


def main() -> int:
    rows, summary = run()

    print(f"{'PaperID':>8}  {'Expected':<11} {'Predicted':<11} {'Pass':<5} "
          f"{'Score':>6}  Reasons")
    print('-' * 100)
    for r in rows:
        print(f"{r['paper_id']:>8}  {r['expected']:<11} {r['predicted']:<11} "
              f"{'OK' if r['pass'] else 'FAIL':<5} {r['score']:>6.3f}  "
              f"{r['reasons']}")

    print()
    print('=' * 60)
    print(f"n                 : {summary['n']}")
    print(f"AUTO_MATCH count  : {summary['auto_match_count']}")
    print(f"REVIEW count      : {summary['review_count']}")
    print(f"REJECT count      : {summary['reject_count']}")
    print(f"True Positive     : {summary['true_positive']}")
    print(f"False Positive    : {summary['false_positive']}")
    print(f"True Negative     : {summary['true_negative']}")
    print(f"False Negative    : {summary['false_negative']}")
    print(f"Precision         : {summary['precision']}")
    print(f"Recall            : {summary['recall']}")
    print(f"All cases pass    : {summary['all_pass']}")
    print()
    print("WARNING: n=8 manually selected benchmark; not representative of "
          "the full 2031-paper population. Precision/Recall above describe "
          "this fixed, hand-verified case set only -- they are NOT a claim "
          "of general production accuracy.")

    return 0 if summary['all_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
