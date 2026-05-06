/**
 * TypeScript models — mirror the Django REST API payloads exactly.
 *
 * Every field name here matches a column in our Postgres views, so the
 * compiler will catch any drift between backend and frontend.
 *
 * Place in: src/app/models/litrix.models.ts
 */

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
  avg_h_index: string;
  max_h_index: number;
}

export interface TopPaper {
  paper_id: number;
  title: string;
  pub_year: number | null;
  doi: string | null;
  source: string | null;
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
  totals: {
    researchers: number;
    active_researchers: number;
    papers: number;
    citations: number;
    q1_papers: number;
    avg_h_index: number;
  };
  top_researchers: ResearcherStats[];
  top_papers: TopPaper[];
  departments: DepartmentStats[];
}

/** DRF pagination wrapper — returned by all list endpoints. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
