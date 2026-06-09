/**
 * Researcher Profile Page.
 *
 * Architecture:
 *   - Reads :userId from the route
 *   - Fetches GET /api/researchers/:id/profile/  (one round-trip)
 *   - Renders 4 sections: identity header, KPI cards, citations chart,
 *     papers list (grouped by year)
 *
 * Why no chart library? An inline SVG bar chart is enough for this
 * simple use-case (10–15 years max), keeps bundle small, and matches
 * the minimalist Apple aesthetic. We can swap to Chart.js later if we
 * need tooltips/legends/etc.
 */
import { Component, OnInit, Input, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';
import {
  ResearcherProfilePayload, ProfilePaper,
} from '../../models/litrix.models';
import { PaperDetailModalComponent } from
  '../paper-detail-modal/paper-detail-modal.component';
import { CitationsChartComponent } from
  '../citations-chart/citations-chart.component';

@Component({
  selector: 'app-researcher-profile',
  standalone: true,
  imports: [CommonModule, PaperDetailModalComponent, CitationsChartComponent],
  templateUrl: './researcher-profile.component.html',
})
export class ResearcherProfileComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly data    = signal<ResearcherProfilePayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  // Paper detail modal — store paper_id; the shared modal fetches full data
  readonly selectedPaperId = signal<number | null>(null);

  // Pagination
  readonly LOAD_BATCH = 15;
  readonly visibleCount = signal<number>(15);

  // Sorting controls (Google Scholar-style toggleable arrows)
  readonly sortField = signal<'year' | 'citations'>('year');
  readonly sortDir   = signal<'asc' | 'desc'>('desc');

  // Quartile filter — multi-select. Empty Set = no filter (show all).
  readonly activeQuartiles = signal<Set<string>>(new Set());

  /**
   * Chart window floor — matches the admin dashboard CHART_YEAR_FLOOR.
   * Anything before this is clipped from the citations chart for visual
   * parity across the app.
   */
  private readonly CHART_YEAR_FLOOR = 2019;

  setSort(field: 'year' | 'citations') {
    if (this.sortField() === field) {
      this.sortDir.update(d => d === 'desc' ? 'asc' : 'desc');
    } else {
      this.sortField.set(field);
      this.sortDir.set('desc');
    }
    this.visibleCount.set(this.LOAD_BATCH);
  }

  toggleQuartile(q: string) {
    this.activeQuartiles.update(s => {
      const next = new Set(s);
      if (next.has(q)) next.delete(q);
      else next.add(q);
      return next;
    });
    this.visibleCount.set(this.LOAD_BATCH);
  }

  // Filter papers by selected quartiles, then sort by chosen field+direction.
  readonly sortedPapers = computed(() => {
    let all = [...(this.data()?.papers ?? [])];

    // Quartile filter — empty Set means "show all" (no filter)
    const quartiles = this.activeQuartiles();
    if (quartiles.size > 0) {
      all = all.filter(p => p.quartile && quartiles.has(p.quartile));
    }

    const field = this.sortField();
    const dir = this.sortDir();
    const mult = dir === 'desc' ? -1 : 1;
    all.sort((a, b) => {
      const av = field === 'year' ? (a.pub_year ?? 0) : (a.citations ?? 0);
      const bv = field === 'year' ? (b.pub_year ?? 0) : (b.citations ?? 0);
      return (av - bv) * mult;
    });
    return all;
  });

  // Visible slice respecting Load More state
  readonly visiblePapers = computed(() =>
    this.sortedPapers().slice(0, this.visibleCount())
  );

  readonly hasMore = computed(() =>
    this.sortedPapers().length > this.visibleCount()
  );

  loadMore() {
    this.visibleCount.update(n => n + this.LOAD_BATCH);
  }

  openPaper(p: ProfilePaper) { this.selectedPaperId.set(p.paper_id); }
  closePaper()              { this.selectedPaperId.set(null); }

  encodeURIComponent(s: string | null | undefined): string {
    return s ? window.encodeURIComponent(s) : '';
  }

  citationsByYearEntries(p: ProfilePaper): { year: string; count: number }[] {
    const cby = p.citations_by_year as any;
    if (!cby || typeof cby !== 'object') return [];
    return Object.entries(cby)
      .map(([year, count]) => ({ year, count: Number(count) || 0 }))
      .sort((a, b) => a.year.localeCompare(b.year));
  }

  // Chart geometry: convert citations_by_year into bar coordinates.
  // Clipped to CHART_YEAR_FLOOR for parity with the admin dashboard.
  readonly chart = computed(() => {
    const cby = (this.data()?.citations_by_year ?? [])
      .filter(c => c.year >= this.CHART_YEAR_FLOOR);
    if (!cby.length) return null;
    const max = Math.max(...cby.map(c => c.citations), 1);
    // Compact chart — about 1/3 the visual weight of the previous size
    const W = 500, H = 110, padding = { top: 8, right: 8, bottom: 22, left: 30 };
    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const barW = innerW / cby.length * 0.65;
    const step = innerW / cby.length;
    return {
      W, H, padding,
      bars: cby.map((c, i) => {
        const h = (c.citations / max) * innerH;
        return {
          year: c.year,
          citations: c.citations,
          x: padding.left + i * step + (step - barW) / 2,
          y: padding.top + innerH - h,
          w: barW,
          h,
        };
      }),
      yLabels: [
        { v: max, y: padding.top },
        { v: Math.round(max / 2), y: padding.top + innerH / 2 },
        { v: 0, y: padding.top + innerH },
      ],
    };
  });

  /**
   * Public Litrix-ID (Lit-NNNNNN) of the researcher to display.
   * Falls back to the :litrixId route param when not set explicitly.
   * The backend resolves Lit-NNNNNN → UserID at the boundary, so this
   * is the only identifier the UI ever has to think about.
   */
  @Input() overrideLitrixId?: string | null;

  ngOnInit() {
    if (this.overrideLitrixId) {
      this.load(this.overrideLitrixId);
      return;
    }
    this.route.params.subscribe(params => {
      const litrixId = params['litrixId'] ?? params['id'];
      if (!litrixId) {
        this.error.set('Invalid researcher ID');
        this.loading.set(false);
        return;
      }
      this.load(litrixId);
    });
  }

  load(id: string | number) {
    this.loading.set(true);
    this.error.set(null);
    this.api.getResearcherProfile(id).subscribe({
      next: payload => {
        // Normalize: backend sometimes returns CitationsByYear as a JSON
        // string (when going through certain views) rather than an
        // object. Parse defensively so the keyvalue pipe never crashes.
        if (payload?.papers) {
          payload.papers = payload.papers.map(p => {
            let cby: any = p.citations_by_year;
            if (typeof cby === 'string') {
              try { cby = JSON.parse(cby); } catch { cby = null; }
            }
            if (cby && typeof cby !== 'object') cby = null;
            return { ...p, citations_by_year: cby };
          });
        }
        this.data.set(payload);
        this.loading.set(false);

        // URL canonicalization. The backend's case/padding-insensitive
        // lookup means the user might land here via /profile/LIT-0001
        // or /profile/lit-1, but the truth in the DB is Lit-000001.
        // Replace the URL silently so the address bar matches the
        // displayed identifier — no history pollution, no flash.
        this.canonicalizeUrl(payload?.identity?.litrix_id);
      },
      error: err => {
        this.error.set(err.status === 404
          ? 'Researcher not found'
          : `Failed to load: ${err.message}`);
        this.loading.set(false);
      },
    });
  }

  private canonicalizeUrl(canonical: string | undefined | null) {
    if (!canonical) return;
    if (this.overrideLitrixId) return;  // hosted inside another page
    const current = this.route.snapshot.paramMap.get('litrixId')
                 ?? this.route.snapshot.paramMap.get('id');
    if (current && current !== canonical) {
      this.router.navigate(['/profile', canonical], { replaceUrl: true });
    }
  }

  fmt(n: number | null | undefined): string {
    if (n == null) return '—';
    return n.toLocaleString('en-US');
  }

  citationsForYear(p: ProfilePaper, year: number): number {
    if (!p.citations_by_year) return 0;
    const v = p.citations_by_year[String(year)];
    return v || 0;
  }
}
