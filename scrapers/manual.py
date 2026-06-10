import os
import sys
import time
import io
import re
import argparse
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import httpx

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()


RESEARCHERS = {
    24: {
        "name": "Elturabi Osman Ahmed Habib",
        "lastname_validators": ["habib"],
        "papers": [
            {"title": "Sustainable Learning of Computer Programming Languages Using Mind Mapping",
             "year": 2023, "journal": "Intelligent Automation and Soft Computing"},
            {"title": "Multi-Class Skin Cancer Detection and Classification Using Hybrid Features Extraction Techniques",
             "year": 2020, "journal": "Journal of Medical Imaging and Health Informatics"},
        ],
    },
    47: {
        "name": "Reem Aljoufi",
        "lastname_validators": ["aljoufi", "joufi"],
        "papers": [
            {"title": "Multi-task Learning for Intrusion Detection and Analysis of Computer Network Traffic", "year": 2021},
            {"title": "MPKMS: A Matrix-based Pairwise Key Management Scheme for Wireless Sensor Networks", "year": 2014},
        ],
    },
    61: {
        "name": "Abdulaziz Alomari",
        "lastname_validators": ["alomari", "omari"],
        "papers": [
            {"title": "Determinants of Medical Internet of Things Adoption in Healthcare and the Role of Demographic Factors Incorporating Modified UTAUT",
             "year": 2023, "doi": "10.14569/IJACSA.2023.0140703",
             "journal": "International Journal of Advanced Computer Science and Applications"},
            {"title": "Knowledge and Perceptions of Hospital Care Staff towards Medical Internet of Things and the Role of Awareness Videos: A Quasi Experimental Study",
             "year": 2023, "doi": "10.4236/ait.2023.134007",
             "journal": "Advances in Internet of Things"},
            {"title": "Influences and Barriers in the Kingdom of Saudi Arabia Affecting Technology Adoption in Healthcare: A Review Paper",
             "year": 2023, "doi": "10.22937/IJCSNS.2023.23.6.7",
             "journal": "International Journal of Computer Science and Network Security"},
            {"title": "Defining the Scope of the Internet of Things with a Particular Focus on Medical Internet of Things",
             "year": 2023,
             "journal": "International Journal of Computer Science and Network Security"},
        ],
    },
    85: {
        "name": "Mohammed Alzahrani (Khadran)",
        "lastname_validators": ["alzahrani", "zahrani"],
        "papers": [
            {"title": "Smart Scholarship Approval System by Using Blockchain and Sentiment Analysis", "year": 2026},
            {"title": "Survey on Multi-Task Learning in Smart Transportation", "year": 2024},
        ],
    },
    86: {
        "name": "Mohammed A Al Qurashi",
        "lastname_validators": ["qurashi", "alqurashi"],
        "papers": [
            {"title": "Energy-Efficient Multi-Factor Authentication for IIoT Devices Using Blockchain Technology", "year": 2026},
            {"title": "Digital Forensics in Cybersecurity: Roles, Responsibilities, and Strategies", "year": 2024},
            {"title": "Harnessing Privacy-Preserving Federated Learning with Blockchain for Secure IoMT Applications in Smart Healthcare Systems", "year": 2024},
            {"title": "Securing Hypervisors in Cloud Computing Environments against Malware Injection", "year": 2023},
            {"title": "Security Tradeoff in Network Virtualization and Their Countermeasures", "year": 2023},
            {"title": "Comparative Analysis of the Use of Pre-Trained Models to Profile Authors Ages and Genders", "year": 2022},
            {"title": "An architecture for resilient intrusion detection in ad-hoc networks", "year": 2020},
            {"title": "An Architecture for Resilient Intrusion Detection in IoT Networks", "year": 2020},
            {"title": "Efficient Intrusion Detection in Ad-Hoc Networks", "year": 2019},
        ],
    },
    99: {
        "name": "Mumdouh Mirghani Mohamed Hassan",
        "lastname_validators": ["mumdouh", "mamdouh", "hassan"],
        "papers": [
            {"title": "Using GIS to Find the Best Safe Route between Khartoum and Arqin-Crossing", "year": 2023},
            {"title": "Smart Sensors and Intelligent Systems: Applications in Engineering Monitoring", "year": 2024},
            {"title": "Investigating the Effects of Organizational E-Portals on Customer Satisfaction", "year": 2022},
            {"title": "Using Online Appraising Performance during Covid 19", "year": 2022},
            {"title": "Using Intelligent Transportation Systems the Modern Traffic Safety on the Highway in the Sudan", "year": 2019},
            {"title": "The Internet of Things (IoT) and its Application Domains", "year": 2019},
            {"title": "The Internet-of-Things (IoT) Security: A Technological Perspective and Review", "year": 2019},
        ],
    },
}


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def normalize_issn(issn):
    if not issn:
        return None
    return ''.join(c for c in str(issn).upper() if c.isalnum())


def title_jaccard(a, b):
    ta = set(normalize_title(a).split())
    tb = set(normalize_title(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def has_lastname_match(authorships, lastnames):
    text = ' '.join(
        (a.get("author", {}).get("display_name") or '').lower()
        for a in (authorships or [])
    )
    text = ''.join(c if c.isalpha() else ' ' for c in text)
    return any(ln.lower() in text for ln in lastnames)


def openalex_get(url, params=None, retries=3):
    p = dict(params or {})
    p["mailto"] = "ra20awn@gmail.com"
    backoff = [10, 30, 60]
    for attempt in range(retries):
        try:
            r = httpx.get(url, params=p, timeout=25)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                time.sleep(backoff[min(attempt, len(backoff)-1)])
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def openalex_by_doi(doi):
    if not doi:
        return None
    return openalex_get(f"https://api.openalex.org/works/doi:{doi.lower()}")


def openalex_search_paper(title, expected_year, lastnames):
    n_target = normalize_title(title)
    if len(n_target) < 15:
        return None
    data = openalex_get(
        "https://api.openalex.org/works",
        {"search": title, "per-page": 10},
    )
    if not data:
        return None
    best = None
    best_score = 0.0
    for w in (data.get("results") or []):
        oa_title = w.get("title") or ""
        jac = title_jaccard(title, oa_title)
        n_oa = normalize_title(oa_title)
        title_ok = (n_oa == n_target) or (jac >= 0.65)
        if not title_ok:
            continue
        oa_year = w.get("publication_year") or 0
        if expected_year and abs(oa_year - expected_year) > 2:
            continue
        if not has_lastname_match(w.get("authorships"), lastnames):
            continue
        if jac > best_score:
            best_score = jac
            best = w
    return best


def find_journal_id(cur, issn, journal_name):
    issn_norm = normalize_issn(issn)
    if issn_norm:
        cur.execute('SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r:
            return r[0]
        cur.execute('SELECT "JournalID" FROM "JournalRankings" WHERE "Issn" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r and r[0]:
            return r[0]
    if journal_name:
        cur.execute(
            'SELECT "JournalID" FROM "Journals" WHERE LOWER("JournalName") = LOWER(%s) LIMIT 1',
            (journal_name,),
        )
        r = cur.fetchone()
        if r:
            return r[0]
    return None


def upsert_paper(cur, p_meta, oa=None):
    title = p_meta["title"].strip()
    norm = normalize_title(title)

    cur.execute(
        'SELECT "PaperID", "DOI", "JournalID" FROM "ResearchPaper" '
        'WHERE "NormalizedTitle" = %s OR LOWER("Title") = LOWER(%s) LIMIT 1',
        (norm, title),
    )
    row = cur.fetchone()

    if oa:
        doi = (oa.get("doi") or "").replace("https://doi.org/", "").lower() or None
        pub_year = oa.get("publication_year") or p_meta.get("year")
        cited = oa.get("cited_by_count") or 0
        primary_loc = oa.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        issn = source.get("issn_l")
        journal_name = source.get("display_name") or p_meta.get("journal")
        authors_str = ", ".join(
            (a.get("author", {}).get("display_name") or "")
            for a in (oa.get("authorships") or [])
        )
    else:
        doi = p_meta.get("doi")
        pub_year = p_meta.get("year")
        cited = 0
        issn = None
        journal_name = p_meta.get("journal")
        authors_str = ""

    raw_log = {
        "title": title, "authors": authors_str,
        "publication": journal_name, "year": pub_year,
        "doi": doi, "issn_l": issn,
        "cited_by_count": cited,
    }

    if row:
        paper_id, existing_doi, existing_journal = row
        jid = existing_journal or find_journal_id(cur, issn, journal_name)
        cur.execute('''
            UPDATE "ResearchPaper"
            SET "DOI"       = COALESCE("DOI", %s),
                "JournalID" = COALESCE("JournalID", %s),
                "RawData_Log" = jsonb_set(
                    COALESCE("RawData_Log", '{}'::jsonb),
                    '{cited_by_count}', to_jsonb(%s::int)
                )
            WHERE "PaperID" = %s
        ''', (doi, jid, cited, paper_id))
        return paper_id, 'enriched' if (doi or jid) else 'existing'

    jid = find_journal_id(cur, issn, journal_name)
    src = 'OpenAlex' if oa else 'Manual'
    try:
        cur.execute('SAVEPOINT sp_paper')
        cur.execute('''
            INSERT INTO "ResearchPaper" (
                "Title", "NormalizedTitle", "PubYear", "DOI", "JournalID",
                "Source", "RawData_Log", "ScrapedAt", "IsVerified"
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), TRUE)
            RETURNING "PaperID"
        ''', (title, norm, pub_year, doi, jid, src, Json(raw_log)))
        paper_id = cur.fetchone()[0]
        cur.execute('RELEASE SAVEPOINT sp_paper')
        return paper_id, 'new'
    except psycopg2.errors.UniqueViolation:
        cur.execute('ROLLBACK TO SAVEPOINT sp_paper')
        cur.execute(
            'SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
            (title,),
        )
        r = cur.fetchone()
        return (r[0] if r else None), 'existing'


def link_author(cur, user_id, paper_id, name_raw):
    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, 1, FALSE, 1.0, 'manual_profile', %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (user_id, paper_id, name_raw or ''))


def process_researcher(conn, uid, info, manual=False):
    cur = conn.cursor()
    cur.execute('SELECT "FullName_Ar" FROM "Users" WHERE "UserID" = %s', (uid,))
    row = cur.fetchone()
    if not row:
        print(f"  UID {uid} not found")
        return
    print(f"\n{'='*60}")
    print(f"UID={uid}  {row[0]}  ({info['name']})")
    print(f"{'='*60}")

    n_oa = n_manual = n_existing = 0
    for p in info["papers"]:
        title = p["title"]
        print(f"\n  • {title[:75]}")

        if manual:
            oa = None
            print(f"    manual mode")
        else:
            time.sleep(0.3)
            if p.get("doi"):
                oa = openalex_by_doi(p["doi"])
            else:
                oa = openalex_search_paper(title, p.get("year"), info["lastname_validators"])
            if oa:
                doi = (oa.get('doi') or '').replace('https://doi.org/', '')
                print(f"    OpenAlex: DOI={doi}, cites={oa.get('cited_by_count') or 0}")
            else:
                print(f"    no match — using manual metadata")

        paper_id, status = upsert_paper(cur, p, oa)
        if not paper_id:
            print(f"    insert failed")
            continue

        link_author(cur, uid, paper_id, info["name"])

        if status == 'new':
            if oa: n_oa += 1
            else:  n_manual += 1
            print(f"    + PaperID={paper_id}  ({'OpenAlex' if oa else 'Manual'})")
        else:
            n_existing += 1
            print(f"    ↺ PaperID={paper_id}  ({status})")

    conn.commit()
    cur.close()
    print(f"\n  Summary: {n_oa} new (OpenAlex), {n_manual} new (manual), {n_existing} existing")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", type=int, default=None)
    ap.add_argument("--manual", action="store_true")
    args = ap.parse_args()

    conn = db()

    if args.uid:
        if args.uid not in RESEARCHERS:
            print(f"UID {args.uid} not in config. Available: {list(RESEARCHERS.keys())}")
            return
        process_researcher(conn, args.uid, RESEARCHERS[args.uid], args.manual)
    else:
        for uid, info in RESEARCHERS.items():
            process_researcher(conn, uid, info, args.manual)

    conn.close()
    print("\nDone")


if __name__ == "__main__":
    main()
