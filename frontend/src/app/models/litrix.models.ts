// These mirror the DRF payloads field-for-field (each name maps to a
// Postgres view column) so the compiler catches backend/frontend drift.

export interface ResearcherStats {
  user_id: number;
  full_name_ar: string | null;
  full_name_en: string | null;
  scholar_id: string | null;
  orcid_id: string | null;
  openalex_author_id: string | null;
  academic_rank: string | null;
  department_id: number | null;
  department_name: string | null;
  total_papers: number;
  papers_last_5_years: number;
  total_citations: number;
  avg_citations_per_paper: string;
  h_index: number;
  first_pub_year: number | null;
  last_pub_year: number | null;
  q1_papers: number;
  cross_validated_papers: number;
  scopus_papers: number;
  isi_papers: number;
  manual_papers: number;
  last_synced_at: string | null;
}

export interface DepartmentStats {
  department_id: number;
  department_name: string;
  college_id: number | null;
  total_researchers: number;
  active_researchers: number;
  total_papers: number;
  total_citations: number;
  total_q1_papers: number;
  total_scopus_papers: number;
  total_isi_papers: number;
  avg_h_index: string;
  max_h_index: number;
}

export interface TopPaper {
  paper_id: number;
  title: string;
  pub_year: number | null;
  doi: string | null;
  source: string | null;
  indexing: string | null;
  citations: number;
  journal_name: string | null;
  quartile: string | null;
  impact_factor: number | null;
  primary_author_ar: string | null;
  scraped_at: string | null;
}

export interface PublicationTrend {
  department_id: number;
  department_name: string;
  year: number;
  papers: number;
  citations: number;
  q1_papers: number;
}

export interface OverviewPayload {
  focus_years: number[];
  totals: {
    researchers: number;
    active_researchers: number;
    papers: number;
    citations: number;
    q1_papers: number;
    q2_papers: number;
    q3_papers: number;
    q4_papers: number;
    scopus_papers: number;
    isi_papers: number;
    avg_h_index: number;
    journal_papers?: number;
    conference_papers?: number;
    book_papers?: number;
    preprint_papers?: number;
    /** Papers in scope not yet verified for Al-Baha affiliation (NULL).
     *  Independent data-quality metric — NOT included in `papers`. */
    pending_review?: number;
  };
  top_researchers: any[];
  top_papers: any[];
  departments: any[];
}

/** DRF pagination wrapper - returned by all list endpoints. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

/** Per-paper details for the yearly-breakdown UI. */
export interface PaperDetail {
  paper_id: number;
  title: string;
  doi: string | null;
  citations: number;
  journal_name: string | null;
  venue_type: 'Journal' | 'Conference' | 'Book' | 'Preprint' | null;
  quartile: string | null;
  impact_factor: number | null;
  indexing: string | null;
  department_id: number;
  department_name: string;
  authors_ar: string | null;
  authors_en: string | null;
}

export interface YearlyDepartmentSummary {
  department_id: number;
  department_name: string;
  journal_papers: number;
  conference_papers: number;
  total_papers: number;
  total_citations: number;
}

export interface YearlyBreakdownPayload {
  year: number;
  departments: YearlyDepartmentSummary[];
  papers: PaperDetail[];
}

export interface ResearcherIdentity {
  user_id: number;
  /** Public Lit-NNNNNN identifier - the canonical reference outside the DB. */
  litrix_id: string;
  full_name_ar: string | null;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  scholar_id: string | null;
  orcid: string | null;
  openalex_author_id: string | null;
  last_synced_at: string | null;
  department_id: number | null;
  department_name: string | null;
  /** Google Scholar profile photo, or null → fall back to initials. */
  photo_url: string | null;
  /** Scholar's "areas of interest" tags, [] when none scraped yet. */
  research_interests: string[];
  /** Exact name Google Scholar shows on this profile - the primary
   *  displayed name everywhere. Null until the researcher's been scraped. */
  scholar_display_name: string | null;
}

export interface ResearcherStatsAgg {
  total_papers: number;
  total_citations: number;
  q1_papers: number;
  scopus_papers: number;
  isi_papers: number;
}

export interface YearCitations {
  year: number;
  citations: number;
}

export interface ProfilePaper {
  paper_id: number;
  title: string;
  doi: string | null;
  pub_year: number | null;
  source: string | null;
  indexing: string | null;
  citations_by_year: Record<string, number> | null;
  citations: number;
  journal_name: string | null;
  issn_print: string | null;
  venue_type: 'Journal' | 'Conference' | null;
  quartile: string | null;
  impact_factor: number | null;
  // Al-Baha affiliation verification: true = confirmed Al-Baha,
  // false = confirmed NOT Al-Baha, null = pending / unverified.
  affiliation_verified: boolean | null;
}

export interface CoAuthor {
  user_id: number | null;
  /** Present only for platform (Al-Baha) members - drives the profile link. */
  litrix_id: string | null;
  name: string;
  photo_url: string | null;
  shared_papers: number;
  is_albaha: boolean;
}

export interface ResearcherProfilePayload {
  identity: ResearcherIdentity;
  stats: ResearcherStatsAgg;
  citations_by_year: YearCitations[];
  papers: ProfilePaper[];
  coauthors: CoAuthor[];
}

/* ----------------------------------------------------------------------
 * Universal search result shapes
 * ---------------------------------------------------------------------- */

export interface SearchProfileResult {
  user_id: number;
  litrix_id: string;
  full_name_ar: string | null;
  full_name_en: string | null;
  user_type: string;
  department_name: string | null;
  papers: number;
  photo_url: string | null;
}

export interface SearchPaperResult {
  paper_id: number;
  title: string;
  title_en: string | null;
  pub_year: number | null;
  doi: string | null;
  indexing: string | null;
  journal_name: string | null;
  quartile: string | null;
  citations: number;
  authors_summary: string | null;
}

// Backs the overview dashboard's Quartile & Indexing donut "view all" modal.
export interface ClassifiedPaper {
  paper_id: number;
  title: string;
  pub_year: number | null;
  doi: string | null;
  indexing: string | null;
  quartile: string | null;
  venue_type: string | null;
  journal_name: string | null;
  citations: number;
  tier: 'Q1' | 'Q2' | 'Q3' | 'Q4' | 'Other';
}
