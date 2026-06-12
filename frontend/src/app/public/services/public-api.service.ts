// Wraps the no-auth /api/public/* endpoints (analytics/public_views.py).
// The single channel every public-dashboard component talks through -
// kept separate from the authenticated LitrixApiService on purpose.
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';

// --- response shapes ---------------------------------------------------

export interface OverviewResponse {
  total_papers: number;
  total_researchers: number;
  total_citations: number;
  q1_papers: number;
  avg_h_index: number;
}

export interface DepartmentSummary {
  department_id: number;
  department_name: string;
  researchers_count: number;
  papers_count: number;
  q1_papers: number;
  total_citations: number;
}

export interface ResearcherSummary {
  user_id: number;
  litrix_id: string;
  full_name_ar: string;
  full_name_en: string;
  department_id: number | null;
  department_name: string | null;
  academic_rank: string | null;
  specialization: string | null;
  h_index: number;
  total_papers: number;
  total_citations: number;
  photo_url: string | null;
}

export interface PaperSummary {
  paper_id: number;
  title: string;
  doi: string | null;
  pub_year: number | null;
  source: string | null;
  citations: number;
  journal_name: string | null;
  quartile: string | null;
  citations_by_year?: Record<string, number> | null;
  impact_factor?: number | null;
}

export interface ResearcherProfile extends ResearcherSummary {
  scopus_id: string | null;
  orcid_id: string | null;
  photo_url: string | null;
  research_interests: string[];
  stats: {
    total_papers: number;
    total_citations: number;
    q1_papers: number;
    first_year: number | null;
    last_year: number | null;
  };
  citations_by_year_chart: { year: number; citations: number }[];
  papers: PaperSummary[];
  coauthors: CoAuthor[];
}

export interface CoAuthor {
  user_id: number | null;
  litrix_id: string | null;
  name: string;
  photo_url: string | null;
  shared_papers: number;
  is_albaha: boolean;
}

export interface TrendPoint {
  year: number;
  papers: number;
  citations: number;
}

// One KPI's payload - three come back per /kpis/ call.
export interface KpiValue {
  value: number;
  label: string;
  // Extra context numbers, which ones present depends on the KPI.
  published_count?: number;
  total_publications?: number;
  total_faculty?: number;
  total_citations?: number;
}

export interface KpisResponse {
  year: number;
  department_id: number | null;
  kpi_13_percent_faculty_published: KpiValue;
  kpi_14_avg_publications_per_faculty: KpiValue;
  kpi_15_avg_citations_per_publication: KpiValue;
}

// --- service -----------------------------------------------------------

@Injectable({ providedIn: 'root' })
export class PublicApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiBaseUrl}/public`;

  /** Year filter accepts 2025, 2026, or 'all'/undefined for both. */
  private yearQuery(year?: number | 'all'): string {
    if (year && year !== 'all') return `?year=${year}`;
    return '';
  }

  overview(year?: number | 'all'): Observable<OverviewResponse> {
    return this.http.get<OverviewResponse>(
      `${this.base}/overview/${this.yearQuery(year)}`,
    );
  }

  departments(year?: number | 'all'): Observable<{ results: DepartmentSummary[] }> {
    return this.http.get<{ results: DepartmentSummary[] }>(
      `${this.base}/departments/${this.yearQuery(year)}`,
    );
  }

  researchers(
    departmentId?: number,
    year?: number | 'all',
  ): Observable<{ results: ResearcherSummary[] }> {
    const params = new URLSearchParams();
    if (departmentId)            params.set('department_id', String(departmentId));
    if (year && year !== 'all')  params.set('year', String(year));
    const qs = params.toString() ? `?${params.toString()}` : '';
    return this.http.get<{ results: ResearcherSummary[] }>(
      `${this.base}/researchers/${qs}`,
    );
  }

  researcherProfile(litrixId: string): Observable<ResearcherProfile> {
    return this.http.get<ResearcherProfile>(
      `${this.base}/researchers/${encodeURIComponent(litrixId)}/profile/`,
    );
  }

  papers(opts: {
    departmentId?: number;
    year?: number;
    quartile?: string;
    page?: number;
    pageSize?: number;
  } = {}): Observable<{
    count: number;
    page: number;
    page_size: number;
    results: PaperSummary[];
  }> {
    const params = new URLSearchParams();
    if (opts.departmentId) params.set('department_id', String(opts.departmentId));
    if (opts.year)         params.set('year', String(opts.year));
    if (opts.quartile)     params.set('quartile', opts.quartile);
    if (opts.page)         params.set('page', String(opts.page));
    if (opts.pageSize)     params.set('page_size', String(opts.pageSize));
    const qs = params.toString() ? `?${params.toString()}` : '';
    return this.http.get<any>(`${this.base}/papers/${qs}`);
  }

  trends(): Observable<{ results: TrendPoint[] }> {
    return this.http.get<{ results: TrendPoint[] }>(`${this.base}/trends/`);
  }

  kpis(year?: number, departmentId?: number): Observable<KpisResponse> {
    const params = new URLSearchParams();
    if (year)         params.set('year', String(year));
    if (departmentId) params.set('department_id', String(departmentId));
    const qs = params.toString() ? `?${params.toString()}` : '';
    return this.http.get<KpisResponse>(`${this.base}/kpis/${qs}`);
  }

  paperDetail(paperId: number): Observable<PaperDetailResponse> {
    return this.http.get<PaperDetailResponse>(
      `${this.base}/papers/${paperId}/detail/`,
    );
  }

  // Builds the export URL. Empty arrays send no filter so the backend
  // default takes over.
  exportExcelUrl(years?: number[], sheets?: string[]): string {
    const qs = new URLSearchParams();
    if (years && years.length)   qs.set('years', years.join(','));
    if (sheets && sheets.length) qs.set('sheets', sheets.join(','));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return `${this.base}/export/excel/${suffix}`;
  }
}

// Rich paper detail payload - used by the click-into-paper modal.
export interface PaperDetailResponse {
  paper_id: number;
  title: string;
  title_en: string | null;
  abstract: string | null;
  doi: string | null;
  pub_year: number | null;
  volume: string | null;
  issue: string | null;
  pages: string | null;
  language: string | null;
  source: string | null;
  citations_by_year: Record<string, number> | null;
  citations: number;
  journal_name: string | null;
  publisher: string | null;
  quartile: string | null;
  impact_factor: number | null;
  category: string | null;
  internal_authors: {
    user_id: number;
    litrix_id: string;
    name: string;
    order: number | null;
    is_corresponding: boolean | null;
  }[];
  external_authors: { name: string; affiliation: string | null }[];
}
