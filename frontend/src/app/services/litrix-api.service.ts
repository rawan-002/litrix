// Single point of contact with the Django backend - keeps the base URL
// and HTTP wiring in one place so components just inject and call.
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';

import {
  ResearcherStats, DepartmentStats, TopPaper,
  PublicationTrend, OverviewPayload, Paginated,
  YearlyBreakdownPayload, ResearcherProfilePayload,
  SearchProfileResult, SearchPaperResult, ClassifiedPaper,
} from '../models/litrix.models';
import { environment } from '../../environments/environment';

@Injectable({ providedIn: 'root' })
export class LitrixApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiBaseUrl;

  listResearchers(opts?: {
    department_id?: number;
    search?: string;
    ordering?: string;
    page?: number;
  }): Observable<Paginated<ResearcherStats>> {
    let params = new HttpParams();
    if (opts?.department_id) params = params.set('department_id', opts.department_id);
    if (opts?.search)        params = params.set('search', opts.search);
    if (opts?.ordering)      params = params.set('ordering', opts.ordering);
    if (opts?.page)          params = params.set('page', opts.page);
    return this.http.get<Paginated<ResearcherStats>>(
      `${this.baseUrl}/researchers/`, { params }
    );
  }

  getResearcher(userId: number): Observable<ResearcherStats> {
    return this.http.get<ResearcherStats>(
      `${this.baseUrl}/researchers/${userId}/`
    );
  }

  getResearcherPapers(userId: number): Observable<any[]> {
    return this.http.get<any[]>(
      `${this.baseUrl}/researchers/${userId}/papers/`
    );
  }

  // Accepts either a Litrix-ID (Lit-NNNNNN) or a numeric UserID - the
  // backend resolves it, and accepting both keeps legacy callers working.
  getResearcherProfile(id: string | number): Observable<ResearcherProfilePayload> {
    return this.http.get<ResearcherProfilePayload>(
      `${this.baseUrl}/researchers/${id}/profile/`
    );
  }

  // TEST endpoint (Moez Krichen's profile only, for now) - runs the same
  // per-paper Al-Baha OpenAlex check affiliation_verifier.py uses, live and
  // on demand, so the profile's Al-Baha filter can be tried against fresh
  // OpenAlex data instead of the stored AffiliationVerified column. See
  // ResearcherViewSet.openalex_live_affiliation_check.
  getOpenAlexLiveAffiliationCheck(id: string | number): Observable<{
    results: Record<string, boolean | null>;
  }> {
    return this.http.get<{ results: Record<string, boolean | null> }>(
      `${this.baseUrl}/researchers/${id}/openalex-live-affiliation-check/`
    );
  }

  searchResearchers(query: string): Observable<{ results: any[] }> {
    let params = new HttpParams().set('q', query);
    return this.http.get<{ results: any[] }>(
      `${this.baseUrl}/researchers/search/`, { params }
    );
  }

  getAllResearchers(): Observable<{ results: any[] }> {
    return this.http.get<{ results: any[] }>(
      `${this.baseUrl}/researchers/all/`
    );
  }

  // Universal search across profiles + papers. The backend gates paper
  // visibility by role (researchers only see papers they're on; admins see
  // all) and filters profiles to Researcher type for restricted users.
  search(q: string, limit = 10, albahaOnly = false, venueType?: string): Observable<{
    profiles: SearchProfileResult[];
    papers:   SearchPaperResult[];
    papers_has_more: boolean;
    has_full_access: boolean;
  }> {
    let params = new HttpParams().set('q', q).set('limit', String(limit));
    if (albahaOnly) params = params.set('affiliation', 'albaha');
    if (venueType) params = params.set('venue_type', venueType);
    return this.http.get<{
      profiles: SearchProfileResult[];
      papers:   SearchPaperResult[];
      papers_has_more: boolean;
      has_full_access: boolean;
    }>(`${this.baseUrl}/search/`, { params });
  }

  getPaperDetail(paperId: number): Observable<any> {
    return this.http.get<any>(
      `${this.baseUrl}/papers/${paperId}/detail/`
    );
  }

  listDepartments(opts?: { ordering?: string; albahaOnly?: boolean }):
    Observable<Paginated<DepartmentStats>>
  {
    let params = new HttpParams();
    if (opts?.ordering) params = params.set('ordering', opts.ordering);
    if (opts?.albahaOnly) params = params.set('affiliation', 'albaha');
    return this.http.get<Paginated<DepartmentStats>>(
      `${this.baseUrl}/departments/`, { params }
    );
  }

  // Researchers in one department, with their full stats.
  getDepartmentResearchers(
    departmentId: number, albahaOnly = false,
  ): Observable<any> {
    let params = new HttpParams();
    if (albahaOnly) params = params.set('affiliation', 'albaha');
    return this.http.get<any>(
      `${this.baseUrl}/departments/${departmentId}/researchers/`, { params }
    );
  }

  topPapers(opts?: { quartile?: string; ordering?: string }):
    Observable<Paginated<TopPaper>> {
    let params = new HttpParams();
    if (opts?.quartile) params = params.set('quartile', opts.quartile);
    if (opts?.ordering) params = params.set('ordering', opts.ordering);
    return this.http.get<Paginated<TopPaper>>(
      `${this.baseUrl}/papers/top/`, { params }
    );
  }

  trends(departmentId?: number): Observable<Paginated<PublicationTrend>> {
    let params = new HttpParams();
    if (departmentId) params = params.set('department_id', departmentId);
    return this.http.get<Paginated<PublicationTrend>>(
      `${this.baseUrl}/trends/`, { params }
    );
  }

  // Dashboard overview. `years` can be undefined (backend FOCUS_YEARS),
  // a single year, or several - multiple years go on the wire as CSV.
  getOverview(
    years?: number | number[],
    albahaOnly = false,
  ): Observable<OverviewPayload> {
    let params = new HttpParams();
    if (years != null) {
      const list = Array.isArray(years) ? years : [years];
      if (list.length > 0) {
        params = params.set('year', list.join(','));
      }
    }
    // Al-Baha-only excludes papers confirmed under a non-Al-Baha
    // affiliation; the default keeps the institution-wide numbers.
    if (albahaOnly) {
      params = params.set('affiliation', 'albaha');
    }
    return this.http.get<OverviewPayload>(
      `${this.baseUrl}/stats/overview/`,
      params.keys().length ? { params } : {}
    );
  }

  // Full paper list backing the overview dashboard's Quartile & Indexing
  // donut - same filters as getOverview() so the modal always matches what's
  // on screen. `tier` narrows to one bucket (Q1/Scopus/ISI/Other); omitted or
  // 'all' returns every classified-or-not paper in scope.
  getClassifiedPapers(opts: {
    years?: number | number[];
    albahaOnly?: boolean;
    departmentId?: number | null;
    tier?: string | null;
    venue?: string | null;
  }): Observable<{ count: number; papers: ClassifiedPaper[] }> {
    let params = new HttpParams();
    if (opts.years != null) {
      const list = Array.isArray(opts.years) ? opts.years : [opts.years];
      if (list.length > 0) params = params.set('year', list.join(','));
    }
    if (opts.albahaOnly) params = params.set('affiliation', 'albaha');
    if (opts.departmentId != null) params = params.set('department_id', opts.departmentId);
    if (opts.tier) params = params.set('tier', opts.tier);
    if (opts.venue) params = params.set('venue', opts.venue);
    return this.http.get<{ count: number; papers: ClassifiedPaper[] }>(
      `${this.baseUrl}/stats/classified-papers/`,
      params.keys().length ? { params } : {}
    );
  }

  getYearlyBreakdown(year: number): Observable<YearlyBreakdownPayload> {
    return this.http.get<YearlyBreakdownPayload>(
      `${this.baseUrl}/yearly-breakdown/`,
      { params: new HttpParams().set('year', year) }
    );
  }

  // --- Reporting campaigns (admin) ---
  // Campaign/Submission shapes are loose `any` for now; we'll move them
  // into ../models/litrix.models once the UI settles.
  listCampaigns(status?: string): Observable<{ campaigns: any[] }> {
    let params: HttpParams | undefined;
    if (status) params = new HttpParams().set('status', status);
    return this.http.get<{ campaigns: any[] }>(
      `${this.baseUrl}/campaigns/`,
      params ? { params } : {}
    );
  }

  createCampaign(payload: {
    title: string;
    description?: string;
    target_years: number[];
    opens_at: string;
    closes_at: string;
    scope_type?: 'all' | 'department' | 'custom';
    scope_filter?: Record<string, unknown>;
  }): Observable<{ campaign_id: number; status: string }> {
    return this.http.post<{ campaign_id: number; status: string }>(
      `${this.baseUrl}/campaigns/`, payload
    );
  }

  getCampaign(id: number): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/campaigns/${id}/`);
  }

  updateCampaign(id: number, patch: Partial<{
    title: string; description: string;
    target_years: number[];
    opens_at: string; closes_at: string;
    scope_type: string; scope_filter: Record<string, unknown>;
  }>): Observable<{ message: string }> {
    return this.http.patch<{ message: string }>(
      `${this.baseUrl}/campaigns/${id}/`, patch
    );
  }

  openCampaign(id: number): Observable<{
    message: string; submissions_created: number; status: string;
  }> {
    return this.http.post<any>(
      `${this.baseUrl}/campaigns/${id}/open/`, {}
    );
  }

  closeCampaign(id: number): Observable<{ message: string; status: string }> {
    return this.http.post<any>(
      `${this.baseUrl}/campaigns/${id}/close/`, {}
    );
  }

  listCampaignSubmissions(id: number): Observable<{ submissions: any[] }> {
    return this.http.get<any>(
      `${this.baseUrl}/campaigns/${id}/submissions/`
    );
  }

  // Admin view of one researcher's submission. Same shape as the
  // researcher's getMySubmission() so the modal can render either.
  getCampaignSubmissionDetail(campaignId: number, submissionId: number)
    : Observable<{
        submission: any; researcher: any; campaign: any;
        papers: any[]; missing: any[];
      }>
  {
    return this.http.get<any>(
      `${this.baseUrl}/campaigns/${campaignId}/submissions/${submissionId}/`
    );
  }

  // Per-campaign xlsx report. Returns a raw Blob (responseType 'blob')
  // since the response is a stream, not JSON, so the caller can download it.
  exportCampaign(id: number): Observable<Blob> {
    return this.http.get(
      `${this.baseUrl}/campaigns/${id}/export/`,
      { responseType: 'blob' }
    );
  }

  // --- My Reports (researcher) ---
  getMyReports(): Observable<{
    submissions: any[]; pending_count: number;
  }> {
    return this.http.get<any>(`${this.baseUrl}/my-reports/`);
  }

  getMySubmission(submissionId: number): Observable<{
    submission: { submission_id: number; status: string;
                  submitted_at: string | null; is_editable: boolean };
    campaign:   { campaign_id: number; title: string;
                  target_years: number[]; opens_at: string;
                  closes_at: string; status: string };
    papers:     any[];
    missing:    any[];
  }> {
    return this.http.get<any>(
      `${this.baseUrl}/my-reports/${submissionId}/`
    );
  }

  recordDecision(
    submissionId: number,
    payload: { paper_id: number; decision: 'confirmed' | 'not_mine'; note?: string }
  ): Observable<{ decision_id: number; decision: string }> {
    return this.http.post<any>(
      `${this.baseUrl}/my-reports/${submissionId}/decisions/`, payload
    );
  }

  addMissingPaper(
    submissionId: number,
    payload: { title: string; year: number; doi?: string; note?: string }
  ): Observable<{ decision_id: number; decision: string }> {
    return this.http.post<any>(
      `${this.baseUrl}/my-reports/${submissionId}/missing/`, payload
    );
  }

  deleteDecision(submissionId: number, decisionId: number)
    : Observable<{ message: string }>
  {
    return this.http.delete<any>(
      `${this.baseUrl}/my-reports/${submissionId}/decisions/${decisionId}/`
    );
  }

  submitMyReport(submissionId: number): Observable<{
    message: string; submission_id: number; submitted_at: string; is_late: boolean;
  }> {
    return this.http.post<any>(
      `${this.baseUrl}/my-reports/${submissionId}/submit/`, {}
    );
  }
}
