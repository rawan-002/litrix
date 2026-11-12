import { TestBed } from '@angular/core/testing';
import { BehaviorSubject } from 'rxjs';
import { ActivatedRoute, convertToParamMap, ParamMap } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { PublicDashboardComponent } from './public-dashboard.component';
import { PublicApiService } from '../services/public-api.service';
import { environment } from '../../../environments/environment';

// Angular's default RouteReuseStrategy reuses this component across
// navigations that match the SAME route config (public/:tenantSlug/dashboard)
// with only :tenantSlug differing - ngOnInit never runs twice in that case,
// only ActivatedRoute.paramMap emits again. These tests simulate exactly
// that reuse (one component instance, a second paramMap emission) rather
// than creating a fresh component per tenant.
describe('PublicDashboardComponent - tenant switch while component is reused', () => {
  let paramMap$: BehaviorSubject<ParamMap>;
  let httpMock: HttpTestingController;

  function setParams(params: Record<string, string>) {
    paramMap$.next(convertToParamMap(params));
  }

  // The component fires several parallel calls per load (overview,
  // departments, researchers, kpis, trends) - each needs a response shaped
  // for its own endpoint, or e.g. trends' {results:[...]} vs overview's
  // flat object will blow up the chart/list rendering that runs on `next`.
  function flushAllPending(overviewPayload = {
    total_papers: 0, total_researchers: 0, total_citations: 0,
    q1_papers: 0, avg_h_index: 0,
  }) {
    const reqs = httpMock.match(() => true);
    reqs.forEach((req) => {
      const url = req.request.url;
      if (url.includes('/trends/') || url.includes('/departments/') || url.includes('/researchers/')) {
        req.flush({ results: [] });
      } else if (url.includes('/kpis/')) {
        req.flush({
          year: 2026, department_id: null,
          kpi_13_percent_faculty_published: { value: 0, label: '' },
          kpi_14_avg_publications_per_faculty: { value: 0, label: '' },
          kpi_15_avg_citations_per_publication: { value: 0, label: '' },
        });
      } else {
        req.flush(overviewPayload);
      }
    });
    return reqs; // already flushed - callers may still inspect .request.url
  }

  beforeEach(() => {
    paramMap$ = new BehaviorSubject<ParamMap>(convertToParamMap({ tenantSlug: 'albaha-ccit' }));
    TestBed.configureTestingModule({
      imports: [PublicDashboardComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { paramMap: paramMap$ } },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('switching :tenantSlug on a reused instance re-points the service at the new tenant', () => {
    const fixture = TestBed.createComponent(PublicDashboardComponent);
    const api = TestBed.inject(PublicApiService);
    fixture.detectChanges(); // ngOnInit - initial load for 'albaha-ccit'
    flushAllPending();

    // Same component instance, new tenant - simulates router reuse.
    setParams({ tenantSlug: 'other-university' });

    const reqs = flushAllPending();
    expect(reqs.length).toBeGreaterThan(0);
    let sawOtherUniversityRequest = false;
    reqs.forEach((req) => {
      if (req.request.url.includes('/public/other-university/')) sawOtherUniversityRequest = true;
      expect(req.request.url).not.toContain('albaha-ccit');
    });
    expect(sawOtherUniversityRequest).toBe(true);

    // The service itself must have followed the switch too, not just this
    // one component's own requests - it's the single choke point every
    // public component shares.
    expect((api as any).tenantSlug).toBe('other-university');
  });

  it('clearing out the previous tenant\'s data before the new tenant loads - no stale cross-tenant leftovers', () => {
    const fixture = TestBed.createComponent(PublicDashboardComponent);
    fixture.detectChanges();
    flushAllPending({
      total_papers: 42, total_researchers: 5, total_citations: 99,
      q1_papers: 2, avg_h_index: 3,
    });
    fixture.detectChanges();
    expect(fixture.componentInstance.overview()?.total_papers).toBe(42);

    // Switch tenant - stale previous-tenant overview must be wiped
    // synchronously, before the new request's response ever arrives.
    setParams({ tenantSlug: 'other-university' });
    expect(fixture.componentInstance.overview()).toBeNull();

    flushAllPending({
      total_papers: 7, total_researchers: 1, total_citations: 1,
      q1_papers: 0, avg_h_index: 0,
    });
    fixture.detectChanges();
    expect(fixture.componentInstance.overview()?.total_papers).toBe(7);
  });

  it('losing the :tenantSlug segment on a reused instance stops calling the API and clears old data', () => {
    const fixture = TestBed.createComponent(PublicDashboardComponent);
    fixture.detectChanges();
    flushAllPending({
      total_papers: 42, total_researchers: 5, total_citations: 99,
      q1_papers: 2, avg_h_index: 3,
    });

    setParams({}); // no tenantSlug at all
    expect(fixture.componentInstance.overview()).toBeNull();
    expect(fixture.componentInstance.loadFailed()).toBe(true);
    httpMock.expectNone(() => true);
  });
});
