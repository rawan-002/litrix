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
import { ActivatedRoute } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';
import {
  ResearcherProfilePayload, ProfilePaper,
} from '../../models/litrix.models';

@Component({
  selector: 'app-researcher-profile',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './researcher-profile.component.html',
})
export class ResearcherProfileComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  private readonly route = inject(ActivatedRoute);

  readonly data    = signal<ResearcherProfilePayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  // Derived: papers grouped by year (newest first)
  readonly papersByYear = computed(() => {
    const papers = this.data()?.papers ?? [];
    const groups = new Map<number, ProfilePaper[]>();
    for (const p of papers) {
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

  // Chart geometry: convert citations_by_year into bar coordinates
  readonly chart = computed(() => {
    const cby = this.data()?.citations_by_year ?? [];
    if (!cby.length) return null;
    const max = Math.max(...cby.map(c => c.citations), 1);
    const W = 600, H = 160, padding = { top: 10, right: 10, bottom: 30, left: 35 };
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
        this.error.set('معرف الباحث غير صحيح');
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
        this.data.set(payload);
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err.status === 404
          ? 'لم يتم العثور على الباحث'
          : `خطأ في التحميل: ${err.message}`);
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
