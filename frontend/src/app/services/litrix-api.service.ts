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

  getResearcherProfile(userId: number): Observable<ResearcherProfilePayload> {
    return this.http.get<ResearcherProfilePayload>(
      `${this.baseUrl}/researchers/${userId}/profile/`
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

  getOverview(year?: number): Observable<OverviewPayload> {
    const params = year
      ? new HttpParams().set('year', year.toString())
      : undefined;
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
