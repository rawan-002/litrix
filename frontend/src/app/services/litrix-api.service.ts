/**
 * Litrix API Service — single point of contact with the Django backend.
 *
 * Why a service (not direct HttpClient calls)?
 *   1. One place to change the base URL when going to production.
 *   2. One place to add an auth interceptor later.
 *   3. Components stay clean — they just inject this and call .getX().
 *
 * Place in: src/app/services/litrix-api.service.ts
 */
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';

import {
  ResearcherStats, DepartmentStats, TopPaper,
  PublicationTrend, OverviewPayload, Paginated,
  YearlyBreakdownPayload, ResearcherProfilePayload,
  SearchProfileResult, SearchPaperResult,
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

  /**
   * Fetch a researcher profile by either their Litrix-ID (Lit-NNNNNN)
   * or numeric UserID. The backend resolves Litrix-ID → UserID at the
   * boundary; we accept both so legacy callers don't break overnight.
   */
  getResearcherProfile(id: string | number): Observable<ResearcherProfilePayload> {
    return this.http.get<ResearcherProfilePayload>(
      `${this.baseUrl}/researchers/${id}/profile/`
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

  /**
   * Universal search across profiles + papers.
   *
   * Backend applies the permission gate:
   *   - Researcher           → only papers with at least one system author
   *   - Admin / Dean / HoD   → all papers (including external authors)
   *
   * Profiles are always returned for matched name/email/litrix_id, with
   * UserType filtered to "Researcher" for restricted users.
   */
  search(q: string): Observable<{
    profiles: SearchProfileResult[];
    papers:   SearchPaperResult[];
    has_full_access: boolean;
  }> {
    const params = new HttpParams().set('q', q);
    return this.http.get<{
      profiles: SearchProfileResult[];
      papers:   SearchPaperResult[];
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

  /**
   * GET /api/departments/<id>/researchers/
   * Returns researchers in this department with their full stats.
   */
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

  /**
   * Fetch the dashboard overview.
   *
   * `years` semantics:
   *   • undefined / empty array → no filter (backend uses FOCUS_YEARS)
   *   • single number           → filter to that year
   *   • array of numbers        → filter to those years (CSV on the wire)
   */
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
    // Al-Baha-only: exclude papers confirmed authored under a non-Al-Baha
    // affiliation. Omitted (default) keeps the institution-wide numbers.
    if (albahaOnly) {
      params = params.set('affiliation', 'albaha');
    }
    return this.http.get<OverviewPayload>(
      `${this.baseUrl}/stats/overview/`,
      params.keys().length ? { params } : {}
    );
  }

  getYearlyBreakdown(year: number): Observable<YearlyBreakdownPayload> {
    return this.http.get<YearlyBreakdownPayload>(
      `${this.baseUrl}/yearly-breakdown/`,
      { params: new HttpParams().set('year', year) }
    );
  }

  // ============================================================
  // Reporting Campaigns — admin endpoints
  // ============================================================
  // The Campaign + Submission shapes are loose `any` for now; once
  // the UI stabilises we'll lift them into ../models/litrix.models.
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

  /**
   * Admin view of a single researcher's submission — papers + missing
   * entries, with each paper's decision status. Same shape as the
   * researcher's `getMySubmission()` so the modal can render either.
   */
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

  /**
   * Download a per-campaign xlsx report. Returns the raw Blob so the
   * caller can trigger a browser download (the streamed response is
   * NOT a JSON payload — set `responseType: 'blob'`).
   */
  exportCampaign(id: number): Observable<Blob> {
    return this.http.get(
      `${this.baseUrl}/campaigns/${id}/export/`,
      { responseType: 'blob' }
    );
  }

  // ============================================================
  // My Reports — researcher endpoints
  // ============================================================
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
