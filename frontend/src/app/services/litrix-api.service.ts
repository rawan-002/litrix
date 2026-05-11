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

  listDepartments(): Observable<Paginated<DepartmentStats>> {
    return this.http.get<Paginated<DepartmentStats>>(
      `${this.baseUrl}/departments/`
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
  getOverview(years?: number | number[]): Observable<OverviewPayload> {
    let params: HttpParams | undefined;
    if (years != null) {
      const list = Array.isArray(years) ? years : [years];
      if (list.length > 0) {
        params = new HttpParams().set('year', list.join(','));
      }
    }
    return this.http.get<OverviewPayload>(
      `${this.baseUrl}/stats/overview/`,
      params ? { params } : {}
    );
  }

  getYearlyBreakdown(year: number): Observable<YearlyBreakdownPayload> {
    return this.http.get<YearlyBreakdownPayload>(
      `${this.baseUrl}/yearly-breakdown/`,
      { params: new HttpParams().set('year', year) }
    );
  }
}
