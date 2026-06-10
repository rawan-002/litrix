/**
 * Overview Dashboard — Apple-style landing page.
 * Year toggle filters all KPI cards. Export button opens a modal with
 * fine-grained options (years + sheets) before triggering the download.
 *
 * Department filter:
 *   • selectedDeptId === null → show every department (institution-wide).
 *   • selectedDeptId === <id> → focus the entire page on that one dept.
 *
 * The filter is applied client-side from the existing OverviewPayload —
 * no extra backend round-trip — so switching feels instantaneous.
 *
 * Charts: four inline-SVG charts laid out in a 2x2 grid sit between the
 * KPI cards and the legacy data tables. They derive their series from
 * `data.departments[*].by_year` so no new endpoint is required.
 */
import {
  Component, OnInit, inject, signal, computed, HostListener, ElementRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpParams } from '@angular/common/http';
import { LitrixApiService } from '../../services/litrix-api.service';
import { AuthService } from '../../core/services/auth.service';
import {
  OverviewPayload, YearlyBreakdownPayload, PaperDetail,
} from '../../models/litrix.models';
import { environment } from '../../../environments/environment';
import { PaperDetailModalComponent } from
  '../paper-detail-modal/paper-detail-modal.component';
import { CitationsChartComponent } from
  '../citations-chart/citations-chart.component';

/**
 * Year filter is now multi-select. An empty Set means "all years"
 * (institution-wide aggregate, what the backend returns when no year
 * param is sent). Otherwise the Set holds the chosen years.
 */

interface DonutSlice {
  label: string;
  value: number;
  color: string;
  pct: number;
  dashOffset: number;
  dashLength: number;
}

@Component({
  selector: 'app-overview-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, PaperDetailModalComponent, CitationsChartComponent],
  templateUrl: './overview-dashboard.component.html',
})
export class OverviewDashboardComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  /** Used to hide HoD-specific sections from the dashboard. */
  protected readonly auth = inject(AuthService);

  /** True when the user has view_dept_researchers but NOT
   *  view_all_researchers - i.e. a HoD. Used to hide the
   *  multi-department panels that don't apply to a HoD view.
   */
  readonly isHoD = computed(() =>
    !this.auth.hasPermission('view_all_researchers')
  );
  private readonly host = inject(ElementRef);
  private readonly http = inject(HttpClient);

  /** Blocks the Export button while a download is in flight. */
  readonly exporting = signal<boolean>(false);

  /** Close the year popover when clicking anywhere outside the host. */
  @HostListener('document:click', ['$event'])
  onDocumentClick(ev: MouseEvent) {
    if (!this.yearMenuOpen()) return;
    const target = ev.target as Node;
    if (!this.host.nativeElement.contains(target)) {
      this.yearMenuOpen.set(false);
    }
  }

  readonly data    = signal<OverviewPayload | null>(null);
  /** True only for the FIRST load (no data yet) → full-screen skeleton. */
  readonly loading = signal<boolean>(true);
  /** True while re-fetching after a filter change — the existing data stays
   *  on screen so the dashboard never blanks; we only show a subtle spinner. */
  readonly refreshing = signal<boolean>(false);
  readonly error   = signal<string | null>(null);

  /** Monotonic request id so an out-of-order (slower) response from an
   *  earlier filter state can't overwrite the latest one. */
  private reqSeq = 0;

  /** Currently selected years. Empty Set = "All Years" (no filter). */
  readonly selectedYears = signal<Set<number>>(new Set());

  /** Whether the year-picker popover is open. */
  readonly yearMenuOpen = signal<boolean>(false);

  /**
   * Affiliation filter. false = all papers (default, institution-wide
   * numbers untouched). true = Al-Baha only — drops papers the verifier
   * confirmed were authored under a non-Al-Baha affiliation
   * (AffiliationVerified = FALSE). Re-queries the backend on toggle so
   * every paper-derived KPI/chart reflects the choice.
   */
  readonly albahaOnly = signal<boolean>(false);

  /**
   * Hover state for each interactive chart. We keep them separate (not
   * one shared signal) so multiple charts can be in flight visually
   * without the SVGs fighting over a single tooltip render slot.
   *
   * Each entry stores: index/key, screen-space anchor, and the lines
   * the tooltip should render. The chart cell renders the tooltip
   * inside its own SVG so positioning is self-contained.
   */
  readonly hoveredPub      = signal<{ year: number; x: number; y: number; value: number } | null>(null);
  readonly hoveredQuartile = signal<string | null>(null);
  readonly hoveredDept     = signal<number | null>(null);

  /** Compact label for the picker button: "All Years", "2026", "2026, 2025", or "N years". */
  readonly yearsLabel = computed<string>(() => {
    const s = this.selectedYears();
    if (s.size === 0) return 'All Years';
    const sorted = [...s].sort((a, b) => b - a);
    if (sorted.length <= 2) return sorted.join(', ');
    return `${sorted.length} years`;
  });

  /** null = "All Departments". A specific department_id focuses the view. */
  readonly selectedDeptId = signal<number | null>(null);

  /** The filtered department list — either all departments or just one. */
  readonly visibleDepartments = computed<any[]>(() => {
    const all = this.data()?.departments ?? [];
    const id = this.selectedDeptId();
    if (id == null) return all;
    return all.filter(d => d.department_id === id);
  });

  /** Recomputed totals respecting the selected department filter. */
  readonly filteredTotals = computed(() => {
    const d = this.data();
    if (!d) {
      return {
        researchers: 0, active_researchers: 0, papers: 0, citations: 0,
        q1_papers: 0, scopus_papers: 0, isi_papers: 0, avg_h_index: 0,
      };
    }
    if (this.selectedDeptId() == null) return d.totals;
    // For a single department, sum across its by_year and use its
    // researcher counts. Avg h-index isn't departmental, so fall back
    // to the institution-wide value.
    const dept = this.visibleDepartments()[0];
    if (!dept) return d.totals;
    const papers = (dept.by_year || []).reduce(
      (a: number, y: any) => a + (y.papers || 0), 0);
    const citations = (dept.by_year || []).reduce(
      (a: number, y: any) => a + (y.citations || 0), 0);
    return {
      researchers:        dept.total_researchers   ?? 0,
      active_researchers: dept.active_researchers  ?? 0,
      papers,
      citations,
      q1_papers:          dept.total_q1_papers     ?? 0,
      scopus_papers:      dept.total_scopus_papers ?? 0,
      isi_papers:         dept.total_isi_papers    ?? 0,
      avg_h_index:        d.totals.avg_h_index,
    };
  });

  /** Top researchers filtered by selected dept (matched on dept name). */
  readonly visibleTopResearchers = computed<any[]>(() => {
    const list = this.data()?.top_researchers ?? [];
    const id = this.selectedDeptId();
    if (id == null) return list;
    const dept = this.data()?.departments?.find((d: any) => d.department_id === id);
    if (!dept) return list;
    return list.filter(r => r.department_name === dept.department_name);
  });

  /** Top papers filtered to the selected dept's authors (best-effort). */
  readonly visibleTopPapers = computed<any[]>(() => {
    const list = this.data()?.top_papers ?? [];
    const id = this.selectedDeptId();
    if (id == null) return list;
    return list.filter(p => p.department_id === id);
  });

  // --------------------------------------------------------------------
  // Charts: SVG geometry computed from filtered data. Inline SVG keeps
  // the bundle lean and matches the rest of the app's chart pattern.
  // --------------------------------------------------------------------

  private bucketByYear(): { year: number; papers: number; citations: number }[] {
    const buckets = new Map<number, { papers: number; citations: number }>();
    for (const dept of this.visibleDepartments()) {
      for (const y of dept.by_year || []) {
        if (y.year == null) continue;
        const cur = buckets.get(y.year) ?? { papers: 0, citations: 0 };
        cur.papers    += y.papers    || 0;
        cur.citations += y.citations || 0;
        buckets.set(y.year, cur);
      }
    }
    return [...buckets.entries()]
      .sort(([a], [b]) => a - b)
      .map(([year, v]) => ({ year, ...v }));
  }

  /** Publications-per-year bar chart. */
  readonly chartPublications = computed(() => {
    const series = this.bucketByYear();
    if (!series.length) return null;
    return this.buildBars(series.map(s => ({ label: s.year, value: s.papers })));
  });

  /**
   * Citations-by-year series for the shared <app-citations-chart>.
   * The chart geometry + hover now live in that one component so the
   * trend looks identical here, on the researcher dashboard, and on the
   * profile page.
   */
  readonly citationSeries = computed(() =>
    this.bucketByYear().map(s => ({ year: s.year, citations: s.citations })),
  );

  /** Department comparison: horizontal bars by total papers. */
  readonly chartDepartments = computed(() => {
    if (this.selectedDeptId() != null) return null;  // only meaningful on "All"
    const list = (this.data()?.departments ?? [])
      .map(d => ({
        id:    d.department_id,
        label: d.department_name as string,
        value: this.deptTotalPapers(d),
      }))
      .filter(x => x.value > 0)
      .sort((a, b) => b.value - a.value)
      .slice(0, 8);
    if (!list.length) return null;
    const max = Math.max(...list.map(x => x.value), 1);
    return list.map(x => ({ ...x, pct: x.value / max }));
  });

  /** Quartile / indexing donut: Q1, Scopus-only, ISI-only, Unindexed. */
  readonly chartQuartile = computed(() => {
    const t = this.filteredTotals();
    const q1     = t.q1_papers || 0;
    const scopus = Math.max(0, (t.scopus_papers || 0) - q1);  // Scopus excluding Q1
    const isi    = Math.max(0, (t.isi_papers    || 0));
    const total  = t.papers || 0;
    const other  = Math.max(0, total - q1 - scopus - isi);
    if (total === 0) return null;

    const order: { label: string; value: number; color: string }[] = [
      { label: 'Q1',        value: q1,     color: '#1c1917' },
      { label: 'Scopus',    value: scopus, color: '#57534e' },
      { label: 'ISI',       value: isi,    color: '#a8a29e' },
      { label: 'Unindexed', value: other,  color: '#e7e5e4' },
    ].filter(s => s.value > 0);

    const radius = 56;
    const circ   = 2 * Math.PI * radius;
    const slices: DonutSlice[] = [];
    let cursor = 0;
    for (const s of order) {
      const pct = s.value / total;
      const len = pct * circ;
      slices.push({
        label: s.label, value: s.value, color: s.color, pct,
        dashOffset: -cursor, dashLength: len,
      });
      cursor += len;
    }
    return { slices, total, circumference: circ, radius };
  });

  private buildBars(
    series: { label: number | string; value: number }[],
    opts: { fillOpacity?: number } = {},
  ) {
    const max = Math.max(...series.map(s => s.value), 1);
    const W = 500, H = 200, padding = { top: 16, right: 12, bottom: 32, left: 36 };
    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const barW = (innerW / series.length) * 0.6;
    const step = innerW / series.length;
    return {
      W, H, padding,
      fillOpacity: opts.fillOpacity ?? 0.85,
      bars: series.map((s, i) => {
        const h = (s.value / max) * innerH;
        return {
          label: s.label,
          value: s.value,
          x: padding.left + i * step + (step - barW) / 2,
          y: padding.top + innerH - h,
          w: barW,
          h,
        };
      }),
      yLabels: [
        { v: max,                         y: padding.top },
        { v: Math.max(1, Math.round(max / 2)), y: padding.top + innerH / 2 },
        { v: 0,                           y: padding.top + innerH },
      ],
    };
  }

  /**
   * Years the dashboard exposes — recent window only (2020 → current).
   * Mirrors backend YEAR_FLOOR so both ends agree on the same window.
   */
  private static readonly YEAR_FLOOR = 2020;

  readonly availableYears: number[] = (() => {
    const current = new Date().getFullYear();
    const out: number[] = [];
    for (let y = current; y >= OverviewDashboardComponent.YEAR_FLOOR; y--) {
      out.push(y);
    }
    return out;
  })();

  readonly selectedYear = signal<number>(new Date().getFullYear());
  readonly yearlyData     = signal<YearlyBreakdownPayload | null>(null);
  readonly yearlyLoading  = signal<boolean>(false);
  readonly expandedDeptId = signal<number | null>(null);

  readonly showExportModal = signal<boolean>(false);

  /**
   * Per-year toggle map for the export modal. Built from `availableYears`
   * so the export window automatically tracks the dashboard's window
   * (and stays in sync if YEAR_FLOOR changes).
   *
   * Default: every year selected. Admin can untick any to narrow the
   * Excel scope without touching the dashboard's main filter.
   */
  exportYears: Record<number, boolean> = {};

  exportSheets = {
    summary:     true,
    departments: true,
    researchers: true,
    journals:    true,
    conferences: true,
  };

  /** Quick helpers for the export modal's "Select all / none" buttons. */
  selectAllExportYears() {
    this.availableYears.forEach(y => this.exportYears[y] = true);
  }

  clearExportYears() {
    this.availableYears.forEach(y => this.exportYears[y] = false);
  }

  ngOnInit() {
    // Pre-tick every available year for the export modal. Done in
    // ngOnInit (not at field-init time) so it runs after Angular has
    // fully constructed the instance.
    this.selectAllExportYears();

    this.loadOverview();
    this.loadYear(this.selectedYear());
  }

  loadOverview() {
    // First load → full skeleton. Subsequent filter changes → keep the
    // current data visible and just flag a background refresh, so toggling
    // years / Al-Baha feels instant instead of blanking the page.
    if (this.data()) this.refreshing.set(true);
    else this.loading.set(true);

    const seq = ++this.reqSeq;
    const years = [...this.selectedYears()];
    this.api.getOverview(years.length ? years : undefined, this.albahaOnly()).subscribe({
      next: (payload) => {
        if (seq !== this.reqSeq) return;   // superseded by a newer request
        this.data.set(payload);
        this.loading.set(false);
        this.refreshing.set(false);
      },
      error: (err) => {
        if (seq !== this.reqSeq) return;
        // Keep showing the last good data on a refresh failure.
        if (!this.data()) this.error.set(`Failed to load: ${err.message}`);
        this.loading.set(false);
        this.refreshing.set(false);
      },
    });
  }

  toggleYearMenu()  { this.yearMenuOpen.update(v => !v); }
  closeYearMenu()   { this.yearMenuOpen.set(false); }

  /** Toggle a single year in/out of the selection. Closes the popover
   *  afterwards — picking a year shouldn't leave the menu hanging open. */
  toggleYear(y: number) {
    this.selectedYears.update(s => {
      const next = new Set(s);
      if (next.has(y)) next.delete(y);
      else next.add(y);
      return next;
    });
    this.closeYearMenu();
    this.loadOverview();
  }

  /** Clear the selection — equivalent to "All Years". */
  clearYears() {
    this.selectedYears.set(new Set());
    this.closeYearMenu();
    this.loadOverview();
  }

  /** Flip the Al-Baha-only affiliation filter and re-query. */
  toggleAffiliation() {
    this.albahaOnly.update(v => !v);
    this.loadOverview();
  }

  isYearSelected(y: number): boolean {
    return this.selectedYears().has(y);
  }

  // -- Chart interaction helpers -----------------------------------

  setHoveredPub(b: { label: string | number; x: number; y: number; value: number } | null) {
    if (!b) { this.hoveredPub.set(null); return; }
    this.hoveredPub.set({
      year: typeof b.label === 'string' ? Number(b.label) : b.label,
      x: b.x, y: b.y, value: b.value,
    });
  }


  /** Click a year-axis chart bar/point to toggle that year in the filter. */
  pickYearFromChart(year: number) {
    this.toggleYear(year);
  }

  setDepartment(idOrAll: number | 'all' | string | null) {
    if (idOrAll === 'all' || idOrAll == null || idOrAll === '') {
      this.selectedDeptId.set(null);
    } else {
      this.selectedDeptId.set(Number(idOrAll));
    }
  }

  loadYear(year: number) {
    this.selectedYear.set(year);
    this.expandedDeptId.set(null);
    this.yearlyLoading.set(true);
    this.api.getYearlyBreakdown(year).subscribe({
      next: (payload) => {
        this.yearlyData.set(payload);
        this.yearlyLoading.set(false);
      },
      error: () => this.yearlyLoading.set(false),
    });
  }

  toggleDept(deptId: number) {
    this.expandedDeptId.set(
      this.expandedDeptId() === deptId ? null : deptId
    );
  }

  // Paper detail modal — store just the paper_id; the modal component
  // fetches the full details on demand.
  readonly selectedPaperId = signal<number | null>(null);

  openPaper(p: { paper_id: number }) { this.selectedPaperId.set(p.paper_id); }
  closePaper()                       { this.selectedPaperId.set(null); }

  papersFor(deptId: number, venueType: 'Journal' | 'Conference'): PaperDetail[] {
    const papers = this.yearlyData()?.papers ?? [];
    return papers.filter(p =>
      p.department_id === deptId && p.venue_type === venueType
    );
  }

  readonly LOAD_BATCH = 10;
  visibleCounts: Record<string, number> = {};

  visibleCount(deptId: number, venueType: 'Journal' | 'Conference'): number {
    return this.visibleCounts[`${deptId}-${venueType}`] || this.LOAD_BATCH;
  }

  loadMore(deptId: number, venueType: 'Journal' | 'Conference'): void {
    const key = `${deptId}-${venueType}`;
    this.visibleCounts[key] = this.visibleCount(deptId, venueType) + this.LOAD_BATCH;
  }

  papersForLimited(deptId: number, venueType: 'Journal' | 'Conference'): PaperDetail[] {
    return this.papersFor(deptId, venueType)
      .slice(0, this.visibleCount(deptId, venueType));
  }

  hasMore(deptId: number, venueType: 'Journal' | 'Conference'): boolean {
    return this.papersFor(deptId, venueType).length > this.visibleCount(deptId, venueType);
  }

  fmt(n: number | null | undefined): string {
    if (n == null) return '—';
    return n.toLocaleString('en-US');
  }

  /** Sum across by_year breakdown — used for the "Total" row in the
   *  per-department mini-table. Avoids storing the total separately. */
  deptTotalPapers(dept: any): number {
    return (dept.by_year || []).reduce(
      (acc: number, y: any) => acc + (y.papers || 0), 0,
    );
  }

  deptTotalCitations(dept: any): number {
    return (dept.by_year || []).reduce(
      (acc: number, y: any) => acc + (y.citations || 0), 0,
    );
  }

  openExportModal()  { this.showExportModal.set(true); }
  closeExportModal() { this.showExportModal.set(false); }

  triggerExport() {
    const years = Object.entries(this.exportYears)
      .filter(([_, v]) => v)
      .map(([y]) => y)
      .join(',');
    const sheets = Object.entries(this.exportSheets)
      .filter(([_, v]) => v)
      .map(([s]) => s)
      .join(',');

    if (!years) {
      alert('Please select at least one year');
      return;
    }
    if (!sheets) {
      alert('Please select at least one sheet');
      return;
    }

    // Why HttpClient instead of window.location.href?
    //   A direct navigation skips Angular's JwtInterceptor, so the
    //   Authorization header is never attached and the backend (rightly)
    //   rejects with 401. We fetch the file as a blob through the
    //   normal HTTP pipeline, then trigger a save via a temporary
    //   anchor — same end result, with auth intact.
    this.exporting.set(true);
    const params = new HttpParams()
      .set('years', years)
      .set('sheets', sheets);

    this.http.get(
      `${environment.apiBaseUrl}/export/excel/`,
      { params, responseType: 'blob', observe: 'response' },
    ).subscribe({
      next: res => {
        const blob = res.body!;
        const filename = this.parseFilename(
          res.headers.get('Content-Disposition'),
        ) || `litrix-export-${new Date().toISOString().slice(0, 10)}.xlsx`;
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
    // Matches: filename="foo.xlsx" or filename=foo.xlsx
    const m = /filename\*?=(?:UTF-8'')?"?([^"]+?)"?(?:;|$)/i.exec(cd);
    return m ? decodeURIComponent(m[1]) : null;
  }

  /** Trigger a browser download of the given blob. */
  private saveBlob(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Release the blob URL on the next tick — some browsers cancel the
    // download if we revoke synchronously.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
