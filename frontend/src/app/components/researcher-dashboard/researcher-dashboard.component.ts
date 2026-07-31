// At-a-glance dashboard for the logged-in researcher: KPIs + charts, no
// paper list (the profile page is the authoritative full archive). Reuses the
// getResearcherProfile endpoint and derives every chart client-side.
import { Component, OnInit, Input, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { LitrixApiService } from '../../services/litrix-api.service';
import { ResearcherProfilePayload } from '../../models/litrix.models';
import { isLatinName, joinEnglishName } from '../../shared/utils/researcher-name';
import { CitationsChartComponent } from '../citations-chart/citations-chart.component';

interface DonutSlice {
  label: string;
  value: number;
  color: string;
  pct: number;
  // pre-computed stroke-dasharray offsets for the SVG donut arcs
  dashOffset: number;
  dashLength: number;
}

@Component({
  selector: 'app-researcher-dashboard',
  standalone: true,
  imports: [CommonModule, CitationsChartComponent],
  templateUrl: './researcher-dashboard.component.html',
})
export class ResearcherDashboardComponent implements OnInit {
  private readonly api = inject(LitrixApiService);

  /** Litrix-ID of the researcher to render. Required. */
  @Input({ required: true }) litrixId!: string;

  readonly data    = signal<ResearcherProfilePayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  readonly hoveredCitYear = signal<number | null>(null);
  readonly hoveredPubYear = signal<number | null>(null);
  readonly hoveredQuartile = signal<string | null>(null);

  // Clip both bar charts before this year, matching the admin dashboard.
  private readonly CHART_YEAR_FLOOR = 2019;

  readonly identityName = computed(() => {
    const id = this.data()?.identity;
    if (!id) return '';
    const fallbackEn = joinEnglishName(id.first_name, id.last_name);
    const en = id.scholar_display_name || fallbackEn;
    return (isLatinName(en) ? en : '') || 'Researcher';
  });

  ngOnInit() {
    this.load();
  }

  load() {
    if (!this.litrixId) {
      this.error.set('Missing Litrix-ID');
      this.loading.set(false);
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    this.api.getResearcherProfile(this.litrixId).subscribe({
      next: payload => {
        this.data.set(payload);
        this.loading.set(false);
      },
      error: err => {
        this.error.set(`Failed to load: ${err.message}`);
        this.loading.set(false);
      },
    });
  }

  fmt(n: number | null | undefined): string {
    if (n == null) return '-';
    return n.toLocaleString('en-US');
  }

  // Largest h with h papers each cited >= h times. Computed here because the
  // profile endpoint doesn't surface it.
  readonly hIndex = computed(() => {
    const papers = this.data()?.papers ?? [];
    const cites = papers
      .map(p => p.citations || 0)
      .sort((a, b) => b - a);
    let h = 0;
    for (let i = 0; i < cites.length; i++) {
      if (cites[i] >= i + 1) h = i + 1;
      else break;
    }
    return h;
  });

  // Citations-per-year bar chart.
  readonly citationsChart = computed(() => {
    const cby = (this.data()?.citations_by_year ?? [])
      .filter(c => c.year >= this.CHART_YEAR_FLOOR);
    if (!cby.length) return null;
    const max = Math.max(...cby.map(c => c.citations), 1);
    const W = 500, H = 220, padding = { top: 16, right: 12, bottom: 32, left: 36 };
    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const barW = (innerW / cby.length) * 0.6;
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

  // Publications-per-year bar chart.
  readonly publicationsChart = computed(() => {
    const papers = this.data()?.papers ?? [];
    if (!papers.length) return null;
    // Skip papers with no year or older than CHART_YEAR_FLOOR.
    const buckets = new Map<number, number>();
    for (const p of papers) {
      if (p.pub_year == null) continue;
      if (p.pub_year < this.CHART_YEAR_FLOOR) continue;
      buckets.set(p.pub_year, (buckets.get(p.pub_year) ?? 0) + 1);
    }
    if (!buckets.size) return null;
    const series = [...buckets.entries()]
      .sort(([a], [b]) => a - b)
      .map(([year, count]) => ({ year, count }));
    const max = Math.max(...series.map(s => s.count), 1);
    const W = 500, H = 220, padding = { top: 16, right: 12, bottom: 32, left: 36 };
    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const barW = (innerW / series.length) * 0.6;
    const step = innerW / series.length;
    return {
      W, H, padding,
      bars: series.map((s, i) => {
        const h = (s.count / max) * innerH;
        return {
          year: s.year,
          count: s.count,
          x: padding.left + i * step + (step - barW) / 2,
          y: padding.top + innerH - h,
          w: barW,
          h,
        };
      }),
      yLabels: [
        { v: max, y: padding.top },
        { v: Math.max(1, Math.round(max / 2)), y: padding.top + innerH / 2 },
        { v: 0, y: padding.top + innerH },
      ],
    };
  });

  // Quartile distribution donut. One circle per slice, all sharing the same
  // circumference, with stroke-dasharray + dashoffset doing the arc work -
  // cleaner than computing arc paths by hand.
  readonly quartileChart = computed(() => {
    const papers = this.data()?.papers ?? [];
    if (!papers.length) return null;

    const counts: Record<string, number> = {
      Q1: 0, Q2: 0, Q3: 0, Q4: 0, Unranked: 0,
    };
    for (const p of papers) {
      const q = p.quartile && /^Q[1-4]$/.test(p.quartile) ? p.quartile : 'Unranked';
      counts[q] = (counts[q] ?? 0) + 1;
    }

    // Stable order for the donut, with quality-first reading.
    const order: { label: string; color: string }[] = [
      { label: 'Q1', color: '#1c1917' },  // ink-900 - the prestige tier
      { label: 'Q2', color: '#57534e' },  // ink-600
      { label: 'Q3', color: '#a8a29e' },  // ink-400
      { label: 'Q4', color: '#d6d3d1' },  // ink-300
      { label: 'Unranked', color: '#f5f5f4' }, // ink-100
    ];

    const total = papers.length;
    const radius = 56;
    const circumference = 2 * Math.PI * radius;

    const slices: DonutSlice[] = [];
    let cursor = 0;
    for (const o of order) {
      const value = counts[o.label] ?? 0;
      if (value === 0) continue;
      const pct = value / total;
      const dashLength = pct * circumference;
      slices.push({
        label: o.label,
        value,
        color: o.color,
        pct,
        dashOffset: -cursor,
        dashLength,
      });
      cursor += dashLength;
    }

    return {
      slices,
      total,
      circumference,
      radius,
    };
  });
}
