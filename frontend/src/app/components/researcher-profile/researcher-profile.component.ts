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
import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';
import {
  ResearcherProfilePayload, ProfilePaper,
} from '../../models/litrix.models';

@Component({
  selector: 'app-researcher-profile',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './researcher-profile.component.html',
})
export class ResearcherProfileComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  private readonly route = inject(ActivatedRoute);

  readonly data    = signal<ResearcherProfilePayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  // Paper detail modal
  readonly selectedPaper = signal<ProfilePaper | null>(null);

  // Pagination for the papers list. Show this many papers max initially;
  // each "Load More" click bumps it by LOAD_BATCH.
  readonly LOAD_BATCH = 15;
  readonly visibleCount = signal<number>(15);

  // Derived: papers grouped by year (newest first), respecting visibleCount.
  // We slice the FULL ordered list, then re-group, so the slice still
  // shows the most-recent papers across all years.
  readonly papersByYear = computed(() => {
    const allPapers = this.data()?.papers ?? [];
    const visible = allPapers.slice(0, this.visibleCount());
    const groups = new Map<number, ProfilePaper[]>();
    for (const p of visible) {
      const y = p.pub_year ?? 0;
      if (!groups.has(y)) groups.set(y, []);
      groups.get(y)!.push(p);
    }
    const sortedYears = Array.from(groups.keys()).sort((a, b) => b - a);
    return sortedYears.map(y => ({
      year: y || null,
      papers: groups.get(y)!,
    }));
  });

  readonly hasMore = computed(() =>
    (this.data()?.papers?.length ?? 0) > this.visibleCount()
  );

  loadMore() {
    this.visibleCount.update(n => n + this.LOAD_BATCH);
  }

  openPaper(p: ProfilePaper) { this.selectedPaper.set(p); }
  closePaper()              { this.selectedPaper.set(null); }

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

  // Chart geometry: convert citations_by_year into bar coordinates
  readonly chart = computed(() => {
    const cby = this.data()?.citations_by_year ?? [];
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

  ngOnInit() {
    this.route.params.subscribe(params => {
      const userId = Number(params['id']);
      if (Number.isNaN(userId)) {
        this.error.set('Invalid researcher ID');
        this.loading.set(false);
        return;
      }
      this.load(userId);
    });
  }

  load(userId: number) {
    this.loading.set(true);
    this.error.set(null);
    this.api.getResearcherProfile(userId).subscribe({
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
      },
      error: err => {
        this.error.set(err.status === 404
          ? 'Researcher not found'
          : `Failed to load: ${err.message}`);
        this.loading.set(false);
      },
    });
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
