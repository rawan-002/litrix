import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { PublicApiService } from './public-api.service';
import { environment } from '../../../environments/environment';

// There is no default tenant. Every one of these pins that: a fresh service
// (or one whose slug was cleared back to null) must refuse to call the
// backend at all - never guess, never fall back to a particular
// university - and switching the slug must never leak into a request built
// for the previous one.
describe('PublicApiService - tenant slug fencing', () => {
  let service: PublicApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(PublicApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('sends no HTTP request and errors when no tenant slug has ever been set', () => {
    let errored = false;
    service.overview().subscribe({
      next: () => { throw new Error('must not succeed without a tenant slug'); },
      error: () => { errored = true; },
    });
    expect(errored).toBe(true);
    httpMock.expectNone(() => true);
  });

  it('refuses every endpoint (not just overview) when no slug is set', () => {
    const errors: boolean[] = [];
    service.departments().subscribe({ error: () => errors.push(true) });
    service.researchers().subscribe({ error: () => errors.push(true) });
    service.trends().subscribe({ error: () => errors.push(true) });
    service.kpis().subscribe({ error: () => errors.push(true) });
    service.papers().subscribe({ error: () => errors.push(true) });
    service.researcherProfile('Lit-000001').subscribe({ error: () => errors.push(true) });
    service.paperDetail(1).subscribe({ error: () => errors.push(true) });
    expect(errors.length).toBe(7);
    httpMock.expectNone(() => true);
  });

  it('exportExcelUrl returns null, never a URL, when no slug is set', () => {
    expect(service.exportExcelUrl()).toBeNull();
  });

  it('an empty/whitespace slug is treated the same as no slug at all', () => {
    service.setTenantSlug('   ');
    let errored = false;
    service.overview().subscribe({ error: () => { errored = true; } });
    expect(errored).toBe(true);
    httpMock.expectNone(() => true);
  });

  it('a request for one slug is never answered by / mixed with another tenant\'s path', () => {
    service.setTenantSlug('albaha-ccit');
    service.overview().subscribe();
    const reqA = httpMock.expectOne(
      `${environment.apiBaseUrl}/public/albaha-ccit/overview/`,
    );
    reqA.flush({
      total_papers: 1, total_researchers: 1, total_citations: 1,
      q1_papers: 0, avg_h_index: 0,
    });

    // Swap to a different tenant slug - the very next call must go to the
    // NEW tenant's path, with no trace of the old one.
    service.setTenantSlug('other-university');
    service.overview().subscribe();
    const reqB = httpMock.expectOne(
      `${environment.apiBaseUrl}/public/other-university/overview/`,
    );
    expect(reqB.request.url).not.toContain('albaha-ccit');
    reqB.flush({
      total_papers: 2, total_researchers: 2, total_citations: 2,
      q1_papers: 0, avg_h_index: 0,
    });
  });

  it('clearing the slug back to null after it was set refuses again - no sticky default', () => {
    service.setTenantSlug('albaha-ccit');
    service.setTenantSlug(null);
    let errored = false;
    service.overview().subscribe({ error: () => { errored = true; } });
    expect(errored).toBe(true);
    httpMock.expectNone(() => true);
  });
});
