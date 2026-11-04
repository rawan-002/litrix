// At-a-glance dashboard for the logged-in researcher: KPIs + charts, no
// paper list (the profile page is the authoritative full archive). Reuses the
// getResearcherProfile endpoint and derives every chart client-side.
import {
  Component, OnInit, Input, inject, signal, computed, effect, untracked,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpParams } from '@angular/common/http';
import { LitrixApiService } from '../../services/litrix-api.service';
import { AffiliationService } from '../../core/services/affiliation.service';
import { ResearcherProfilePayload } from '../../models/litrix.models';
import { isLatinName, joinEnglishName } from '../../shared/utils/researcher-name';
import { CitationsChartComponent } from '../citations-chart/citations-chart.component';
import { PaperDetailModalComponent } from '../paper-detail-modal/paper-detail-modal.component';
import { environment } from '../../../environments/environment';

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
  imports: [CommonModule, FormsModule, CitationsChartComponent, PaperDetailModalComponent],
  templateUrl: './researcher-dashboard.component.html',
})
export class ResearcherDashboardComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  private readonly affiliation = inject(AffiliationService);
  private readonly http = inject(HttpClient);

  /** Blocks the Export button while a download is in flight - same name/
   *  purpose as the admin dashboard's. */
  readonly exporting = signal(false);

  /** Litrix-ID of the researcher to render. Required. */
  @Input({ required: true }) litrixId!: string;

  // Al-Baha filter, identical rule to the profile page so the two never
  // disagree: when the header toggle is ON, keep only papers CONFIRMED
  // Al-Baha (affiliation_verified === true) - foreign + still-unverified are
  // both hidden. Every KPI/chart below reads statsPapers(), so flipping the
  // toggle recomputes them instantly (no re-fetch; the payload has all papers).
  readonly statsPapers = computed(() => {
    const all = this.data()?.papers ?? [];
    return this.affiliation.albahaOnly()
      ? all.filter(p => p.affiliation_verified === true)
      : all;
  });

  readonly kpiPublications = computed(() => this.statsPapers().length);
  readonly kpiCitations = computed(() =>
    this.statsPapers().reduce((s, p) => s + (p.citations ?? 0), 0)
  );
  readonly kpiQ1 = computed(() =>
    this.statsPapers().filter(p => p.quartile === 'Q1').length
  );

  readonly data    = signal<ResearcherProfilePayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  // "View all classified papers" modal - same interaction as the admin
  // overview dashboard's donut (openClassifiedModal/classifiedTier/
  // classifiedSearch/showMoreClassifiedPapers), just filtered client-side
  // from statsPapers() (already fully loaded) instead of an API call.
  readonly classifiedModalOpen = signal(false);
  readonly classifiedTier      = signal<string | null>(null);
  readonly classifiedSearch    = signal('');
  readonly CLASSIFIED_BATCH     = 40;
  readonly classifiedShown      = signal(this.CLASSIFIED_BATCH);

  readonly classifiedTierPapers = computed(() => {
    const tier = this.classifiedTier();
    const list = this.statsPapers();
    if (!tier) return list;
    if (tier === 'Other') return list.filter(p => !p.quartile || !/^Q[1-4]$/.test(p.quartile));
    return list.filter(p => p.quartile === tier);
  });

  readonly filteredClassifiedPapers = computed(() => {
    const q = this.classifiedSearch().trim().toLowerCase();
    const list = this.classifiedTierPapers();
    if (!q) return list;
    return list.filter(p =>
      p.title.toLowerCase().includes(q) ||
      (p.journal_name || '').toLowerCase().includes(q)
    );
  });

  readonly visibleClassifiedPapers = computed(() =>
    this.filteredClassifiedPapers().slice(0, this.classifiedShown())
  );

  private readonly classifiedResetEffect = effect(() => {
    this.classifiedTierPapers();
    this.classifiedSearch();
    untracked(() => this.classifiedShown.set(this.CLASSIFIED_BATCH));
  });

  showMoreClassifiedPapers() {
    this.classifiedShown.update(n => n + this.CLASSIFIED_BATCH);
  }

  /** `tier` is a donut slice label ('Q1'/'Q2'/'Q3'/'Q4'/'Unranked'), mapped to
   *  'Other' to match the admin modal's "Unindexed" option value, or omitted
   *  to show every paper ("view all papers" link). */
  openClassifiedModal(tier?: string) {
    this.classifiedSearch.set('');
    this.classifiedTier.set(tier === 'Unranked' ? 'Other' : (tier ?? null));
    this.classifiedModalOpen.set(true);
  }

  closeClassifiedModal() {
    this.classifiedModalOpen.set(false);
  }

  // Opens the shared paper-detail modal, same as the admin dashboard / search.
  readonly selectedPaperId = signal<number | null>(null);
  openPaper(p: { paper_id: number }) { this.selectedPaperId.set(p.paper_id); }
  closePaper() { this.selectedPaperId.set(null); }

  // --- Export modal - same years-picker interaction as the admin
  // dashboard's (selectAllExportYears/clearExportYears/exporting), with a
  // file-format choice standing in for the admin modal's institution-wide
  // sheet picker (Summary/Departments/Researchers/...), which doesn't apply
  // to a single person's data. ---
  readonly showExportModal = signal(false);
  exportYears: Record<number, boolean> = {};
  readonly exportFormat = signal<'excel' | 'json' | 'csv'>('excel');

  /** Years that actually appear in this researcher's own papers, newest
   *  first - the personal-dashboard equivalent of the admin modal's
   *  availableYears (which there comes from the whole institution). */
  readonly availableExportYears = computed(() => {
    const years = new Set<number>();
    for (const p of this.statsPapers()) {
      if (p.pub_year != null) years.add(p.pub_year);
    }
    return [...years].sort((a, b) => b - a);
  });

  selectAllExportYears() {
    this.availableExportYears().forEach(y => this.exportYears[y] = true);
  }

  clearExportYears() {
    this.availableExportYears().forEach(y => this.exportYears[y] = false);
  }

  openExportModal() {
    this.selectAllExportYears();
    this.exportFormat.set('excel');
    this.showExportModal.set(true);
  }

  closeExportModal() {
    this.showExportModal.set(false);
  }

  // Blob-download pattern, same as the admin dashboard's triggerExport()
  // (HttpClient, not a direct navigation, so the auth header actually goes
  // out instead of 401ing).
  triggerExport() {
    const years = Object.entries(this.exportYears)
      .filter(([_, v]) => v)
      .map(([y]) => y)
      .join(',');

    if (!years) {
      alert('Please select at least one year');
      return;
    }

    this.exporting.set(true);
    const params = new HttpParams()
      .set('years', years)
      .set('filetype', this.exportFormat());

    this.http.get(
      `${environment.apiBaseUrl}/export/my-papers/`,
      { params, responseType: 'blob', observe: 'response' },
    ).subscribe({
      next: res => {
        const blob = res.body!;
        const filename = this.parseFilename(
          res.headers.get('Content-Disposition'),
        ) || `my-papers-${new Date().toISOString().slice(0, 10)}.${this.exportFormat()}`;
        this.saveBlob(blob, filename);
        this.exporting.set(false);
        this.closeExportModal();
      },
      error: err => {
        this.exporting.set(false);
        const msg = err?.error?.detail
                 || err?.message
                 || `Export failed (HTTP ${err?.status ?? '?'})`;
        alert(msg);
      },
    });
  }

  /** Pull the filename out of a Content-Disposition header, if present. */
  private parseFilename(cd: string | null): string | null {
    if (!cd) return null;
    const m = /filename\*?=(?:UTF-8'')?"?([^"]+?)"?(?:;|$)/i.exec(cd);
    return m ? decodeURIComponent(m[1]) : null;
  }

  private saveBlob(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

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
    const papers = this.statsPapers();
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
    const papers = this.statsPapers();
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
    const papers = this.statsPapers();
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
