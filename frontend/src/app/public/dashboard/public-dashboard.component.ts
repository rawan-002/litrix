// Read-only, single-page overview of the college's research output:
// hero KPIs, department cards, a yearly trend chart, and a filterable
// researcher list. No login chrome, no admin actions.
import {
  Component, OnInit, signal, computed, inject, ViewChild,
  ElementRef, AfterViewInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Chart, registerables } from 'chart.js';
import {
  PublicApiService, OverviewResponse, DepartmentSummary,
  ResearcherSummary, TrendPoint, KpisResponse,
} from '../services/public-api.service';

Chart.register(...registerables);

@Component({
  selector: 'app-public-dashboard',
  standalone: true,
  imports: [CommonModule, RouterLink, FormsModule],
  template: `
    <div class="min-h-screen bg-white">
      <!-- Header -->
      <header class="border-b border-gray-100 bg-gradient-to-b from-gray-50/50 to-white">
        <div class="max-w-7xl mx-auto px-6 py-12 flex items-start justify-between gap-4">
          <div>
            <h1 class="text-2xl md:text-3xl font-semibold text-gray-900 tracking-tight">
              College of Computing & Information Technology
            </h1>
            <p class="mt-2 text-sm text-gray-500">
              Research Output Dashboard
              <span class="ml-2 text-gray-400">· {{ activeYearLabel() }}</span>
            </p>

            <!-- Year filter + export -->
            <div class="mt-4 flex flex-wrap items-center gap-3">
              <div class="inline-flex items-center gap-1 p-1 rounded-xl bg-gray-100">
                <button *ngFor="let opt of yearOptions"
                        type="button"
                        (click)="setGlobalYear(opt.value)"
                        class="px-4 py-1.5 rounded-lg text-sm font-medium transition-all"
                        [ngClass]="globalYear() === opt.value
                          ? 'bg-white text-gray-900 shadow-sm'
                          : 'text-gray-500 hover:text-gray-900'">
                  {{ opt.label }}
                </button>
              </div>

              <button (click)="openExportModal()"
                 class="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium shadow-sm hover:shadow-md transition-all whitespace-nowrap">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"/>
                </svg>
                Export Excel
              </button>
            </div>
          </div>

          <!-- Logo, top-right -->
          <img src="litrix-logo.png"
               alt="Litrix"
               class="h-14 md:h-16 w-auto select-none shrink-0"
               draggable="false" />
        </div>
      </header>

      <!-- Hero KPIs -->
      <section class="max-w-7xl mx-auto px-6 py-14">
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 md:gap-6">
          <div *ngFor="let kpi of kpiCards()"
               class="rounded-2xl bg-gradient-to-br from-gray-50 to-white border border-gray-100 p-6 md:p-8 hover:shadow-md transition-shadow">
            <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {{ kpi.label }}
            </p>
            <p class="mt-4 text-4xl md:text-5xl font-semibold text-gray-900 tracking-tight">
              {{ kpi.value | number }}
            </p>
            <p class="mt-2 text-xs text-gray-400">{{ kpi.sub }}</p>
          </div>
        </div>
      </section>

      <!-- Departments -->
      <section class="max-w-7xl mx-auto px-6 py-12">
        <h2 class="text-2xl font-semibold text-gray-900 mb-8">Departments</h2>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          <div *ngFor="let d of departments()"
               class="rounded-2xl border border-gray-100 p-6 hover:shadow-lg hover:border-indigo-100 hover:-translate-y-0.5 transition-all cursor-pointer group"
               (click)="filterByDepartment(d.department_id)">
            <h3 class="text-lg font-semibold text-gray-900 mb-5 min-h-[3.5rem] group-hover:text-indigo-700 transition-colors">
              {{ d.department_name }}
            </h3>
            <div class="space-y-3">
              <div class="flex justify-between items-baseline">
                <span class="text-sm text-gray-500">Researchers</span>
                <span class="font-semibold text-gray-900 text-lg">{{ d.researchers_count }}</span>
              </div>
              <div class="flex justify-between items-baseline">
                <span class="text-sm text-gray-500">Publications</span>
                <span class="font-semibold text-gray-900 text-lg">{{ d.papers_count | number }}</span>
              </div>
              <div class="flex justify-between items-baseline">
                <span class="text-sm text-gray-500">Q1 Papers</span>
                <span class="font-semibold text-indigo-600 text-lg">{{ d.q1_papers | number }}</span>
              </div>
              <div class="flex justify-between items-baseline">
                <span class="text-sm text-gray-500">Citations</span>
                <span class="font-semibold text-gray-900 text-lg">{{ d.total_citations | number }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Performance metrics — hidden for now; flip [hidden]="true" to show. -->
      <section [hidden]="true" class="max-w-7xl mx-auto px-6 py-12">
        <div class="mb-8">
          <h2 class="text-2xl font-semibold text-gray-900">Performance Metrics</h2>
          <p class="mt-1 text-sm text-gray-500">
            Academic KPIs (NCAAA-style) — scoped by the year filter above.
          </p>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6">
          <!-- % faculty published -->
          <div *ngIf="kpis() as k" class="rounded-2xl border border-gray-100 p-6 md:p-8">
            <div class="flex items-center justify-between">
              <p class="text-sm font-medium text-gray-500 uppercase tracking-wider">
                {{ k.kpi_13_percent_faculty_published.label }}
              </p>
              <span class="text-xs font-mono text-gray-300" title="Scope: selected year">KPI-I-13 · {{ k.year }}</span>
            </div>
            <p class="mt-4 text-5xl font-semibold text-indigo-600 tracking-tight">
              {{ k.kpi_13_percent_faculty_published.value }}%
            </p>
            <div class="mt-4 h-2 rounded-full bg-gray-100 overflow-hidden">
              <div class="h-full bg-indigo-500 transition-all duration-700"
                   [style.width.%]="k.kpi_13_percent_faculty_published.value"></div>
            </div>
            <p class="mt-3 text-sm text-gray-500">
              {{ k.kpi_13_percent_faculty_published.published_count }}
              of {{ k.kpi_13_percent_faculty_published.total_faculty }} faculty published in {{ k.year }}
            </p>
          </div>

          <!-- Avg publications per faculty -->
          <div *ngIf="kpis() as k" class="rounded-2xl border border-gray-100 p-6 md:p-8">
            <div class="flex items-center justify-between">
              <p class="text-sm font-medium text-gray-500 uppercase tracking-wider">
                {{ k.kpi_14_avg_publications_per_faculty.label }}
              </p>
              <span class="text-xs font-mono text-gray-300" title="Scope: selected year">KPI-I-14 · {{ k.year }}</span>
            </div>
            <p class="mt-4 text-5xl font-semibold text-teal-600 tracking-tight">
              {{ k.kpi_14_avg_publications_per_faculty.value }}
            </p>
            <div class="mt-4 h-2 rounded-full bg-gray-100 overflow-hidden">
              <!-- 5 pubs/faculty fills the bar -->
              <div class="h-full bg-teal-500 transition-all duration-700"
                   [style.width.%]="kpiBarWidth(k.kpi_14_avg_publications_per_faculty.value, 5)"></div>
            </div>
            <p class="mt-3 text-sm text-gray-500">
              {{ k.kpi_14_avg_publications_per_faculty.total_publications | number }} publications
              ÷ {{ k.kpi_14_avg_publications_per_faculty.total_faculty }} faculty
            </p>
          </div>

          <!-- Avg citations per publication — lifetime (NCAAA standard) -->
          <div *ngIf="kpis() as k" class="rounded-2xl border border-gray-100 p-6 md:p-8">
            <div class="flex items-center justify-between">
              <p class="text-sm font-medium text-gray-500 uppercase tracking-wider">
                {{ k.kpi_15_avg_citations_per_publication.label }}
              </p>
              <span class="text-xs font-mono text-gray-300"
                    title="Lifetime metric — citations accumulate over years, so we measure across all publication years for a realistic average.">
                KPI-I-15 · Lifetime
              </span>
            </div>
            <p class="mt-4 text-5xl font-semibold text-amber-600 tracking-tight">
              {{ k.kpi_15_avg_citations_per_publication.value }}
            </p>
            <div class="mt-4 h-2 rounded-full bg-gray-100 overflow-hidden">
              <!-- 50 cites/pub fills the bar -->
              <div class="h-full bg-amber-500 transition-all duration-700"
                   [style.width.%]="kpiBarWidth(k.kpi_15_avg_citations_per_publication.value, 50)"></div>
            </div>
            <p class="mt-3 text-sm text-gray-500">
              {{ k.kpi_15_avg_citations_per_publication.total_citations | number }} citations
              ÷ {{ k.kpi_15_avg_citations_per_publication.total_publications | number }} publications
            </p>
          </div>
        </div>

        <!-- Loading shimmer (stops on fetch failure) -->
        <div *ngIf="!kpis() && !loadFailed()" class="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div *ngFor="let i of [1,2,3]" class="rounded-2xl border border-gray-100 p-8 animate-pulse">
            <div class="h-4 bg-gray-100 rounded w-2/3 mb-4"></div>
            <div class="h-12 bg-gray-100 rounded w-1/2 mb-4"></div>
            <div class="h-2 bg-gray-100 rounded w-full"></div>
          </div>
        </div>
        <!-- Fetch failed -->
        <div *ngIf="loadFailed()" class="rounded-2xl border border-gray-100 p-8 text-center text-sm text-gray-500">
          Couldn't load the dashboard data. Please refresh the page.
        </div>
      </section>

      <!-- Trend chart -->
      <section class="max-w-7xl mx-auto px-6 py-12">
        <h2 class="text-2xl font-semibold text-gray-900 mb-6">Publications by Year</h2>
        <div class="rounded-2xl border border-gray-100 p-6 max-w-md">
          <canvas #trendCanvas></canvas>
        </div>
      </section>

      <!-- Researchers -->
      <section class="max-w-7xl mx-auto px-6 py-12">
        <div class="flex flex-col md:flex-row md:items-center md:justify-between mb-8 gap-4">
          <h2 class="text-2xl font-semibold text-gray-900">Researchers</h2>
          <div class="flex items-center gap-3">
            <label class="text-sm text-gray-500">Filter by department:</label>
            <select [ngModel]="selectedDepartmentId()"
                    (ngModelChange)="setDepartmentFilter($event)"
                    class="rounded-lg border border-gray-200 px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:border-transparent">
              <option [ngValue]="null">All Departments</option>
              <option *ngFor="let d of departments()" [ngValue]="d.department_id">
                {{ d.department_name }}
              </option>
            </select>
          </div>
        </div>

        <div class="rounded-2xl border border-gray-100 overflow-hidden">
          <table class="w-full">
            <thead class="bg-gray-50">
              <tr>
                <th class="px-6 py-4 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Name
                </th>
                <th class="px-6 py-4 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Department
                </th>
                <th class="px-6 py-4 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Rank
                </th>
                <th class="px-6 py-4 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Papers
                </th>
                <th class="px-6 py-4 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Citations
                </th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
              <tr *ngFor="let r of filteredResearchers()"
                  [routerLink]="['/public/researcher', r.litrix_id]"
                  class="hover:bg-indigo-50/30 cursor-pointer transition-colors group">
                <td class="px-6 py-4">
                  <p class="font-medium text-gray-900 group-hover:text-indigo-700 transition-colors">
                    {{ r.full_name_ar }}
                  </p>
                  <p class="text-xs text-gray-400 font-mono mt-0.5">{{ r.litrix_id }}</p>
                </td>
                <td class="px-6 py-4 text-sm text-gray-700">
                  {{ r.department_name || '—' }}
                </td>
                <td class="px-6 py-4 text-sm text-gray-500">
                  {{ r.academic_rank || '—' }}
                </td>
                <td class="px-6 py-4 text-right font-semibold text-gray-900">
                  {{ r.total_papers | number }}
                </td>
                <td class="px-6 py-4 text-right text-gray-700">
                  {{ r.total_citations | number }}
                </td>
              </tr>
            </tbody>
          </table>
          <div *ngIf="filteredResearchers().length === 0" class="p-12 text-center text-gray-400">
            No researchers found for this filter.
          </div>
        </div>
        <p class="mt-4 text-sm text-gray-400 text-center">
          Showing {{ filteredResearchers().length }} of {{ researchers().length }} researchers
        </p>
      </section>

      <!-- Footer -->
      <footer class="border-t border-gray-100 mt-16">
        <div class="max-w-7xl mx-auto px-6 py-8 text-center text-sm text-gray-400">
          Litrix Research Analytics · College of Computing & IT · Al-Baha University
        </div>
      </footer>

      <!-- Export modal -->
      <div *ngIf="exportModalOpen()"
           class="fixed inset-0 z-50 flex items-center justify-center px-4 py-8"
           (click)="closeExportModal()">
        <div class="absolute inset-0 bg-gray-900/60 backdrop-blur-sm"></div>

        <div class="relative bg-white rounded-3xl shadow-2xl max-w-lg w-full overflow-hidden"
             (click)="$event.stopPropagation()">
          <button (click)="closeExportModal()"
                  class="absolute top-4 right-4 w-9 h-9 rounded-full bg-gray-100 hover:bg-gray-200 flex items-center justify-center text-gray-500 transition-colors"
                  aria-label="Close">✕</button>

          <div class="p-8">
            <h2 class="text-2xl font-semibold text-gray-900 tracking-tight">Export to Excel</h2>
            <p class="mt-1 text-sm text-gray-500">Pick the years and sheets you want to include.</p>

            <!-- Years -->
            <div class="mt-6">
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Years</p>
              <div class="flex flex-wrap gap-2">
                <button *ngFor="let y of availableYears"
                        type="button"
                        (click)="toggleExportYear(y)"
                        class="px-4 py-2 rounded-xl border transition-all text-sm font-medium"
                        [ngClass]="exportYears().includes(y)
                          ? 'bg-indigo-600 text-white border-indigo-600 shadow-sm'
                          : 'bg-white text-gray-700 border-gray-200 hover:border-indigo-300'">
                  {{ y }}
                </button>
              </div>
            </div>

            <!-- Sheets -->
            <div class="mt-6">
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Sheets</p>
              <div class="space-y-2">
                <label *ngFor="let s of exportSheetOptions"
                       class="flex items-center gap-3 p-3 rounded-xl border border-gray-100 hover:bg-gray-50 cursor-pointer transition-colors">
                  <input type="checkbox"
                         [checked]="exportSheets().includes(s.id)"
                         (change)="toggleExportSheet(s.id)"
                         class="w-4 h-4 rounded text-indigo-600 focus:ring-indigo-500 border-gray-300">
                  <div class="flex-1">
                    <p class="text-sm font-medium text-gray-900">{{ s.label }}</p>
                    <p class="text-xs text-gray-500">{{ s.description }}</p>
                  </div>
                </label>
              </div>
            </div>

            <!-- Actions -->
            <div class="mt-8 flex items-center justify-end gap-3">
              <button (click)="closeExportModal()"
                      class="px-5 py-2.5 rounded-xl text-sm font-medium text-gray-600 hover:bg-gray-100 transition-colors">
                Cancel
              </button>
              <button (click)="downloadExport()"
                      [disabled]="!exportIsValid()"
                      class="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-300 disabled:cursor-not-allowed text-white text-sm font-medium shadow-sm transition-all">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"/>
                </svg>
                Download
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class PublicDashboardComponent implements OnInit, AfterViewInit {
  private readonly api = inject(PublicApiService);

  // --- state (signals) -----------------------------------------------
  readonly overview = signal<OverviewResponse | null>(null);
  readonly departments = signal<DepartmentSummary[]>([]);
  readonly researchers = signal<ResearcherSummary[]>([]);
  readonly trendData = signal<TrendPoint[]>([]);
  readonly selectedDepartmentId = signal<number | null>(null);

  // Year filter driving Overview/Departments/Researchers/KPIs.
  // Mirrors DASHBOARD_YEARS in public_views.py.
  readonly globalYear = signal<number | 'all'>('all');
  readonly yearOptions: { label: string; value: number | 'all' }[] = [
    { label: 'All',  value: 'all' },
    { label: '2025', value: 2025 },
    { label: '2026', value: 2026 },
  ];
  readonly activeYearLabel = computed(() => {
    const y = this.globalYear();
    return y === 'all' ? '2025 – 2026 Biennium' : `${y} Snapshot`;
  });

  // KPIs follow globalYear, but need a concrete anchor — they fall back
  // to 2026 when the filter is 'all'.
  readonly kpis = signal<KpisResponse | null>(null);
  // Stops the KPI shimmer and shows a "couldn't load" note on fetch failure.
  readonly loadFailed = signal<boolean>(false);
  readonly availableYears: number[] = [2026, 2025];

  // ---- Export modal state ------------------------------------------
  readonly exportModalOpen = signal<boolean>(false);
  readonly exportYears = signal<number[]>([...this.availableYears]);
  readonly exportSheets = signal<string[]>(
    ['overview', 'summary', 'departments', 'researchers', 'journals', 'conferences'],
  );
  // Same labels and order as the admin export's sheet menu.
  readonly exportSheetOptions = [
    {
      id: 'overview',
      label: 'Overview',
      description: 'KPI snapshot — researchers, publications, citations, h-index',
    },
    {
      id: 'summary',
      label: 'Summary (per year)',
      description: 'Year-by-year totals with Q1-Q4 breakdown',
    },
    {
      id: 'departments',
      label: 'Departments (per year)',
      description: 'Per-department journal/conference split + Q1-Q4',
    },
    {
      id: 'researchers',
      label: 'Researchers',
      description: 'Faculty list with window + all-time + h-index',
    },
    {
      id: 'journals',
      label: 'Journals (per year)',
      description: 'Full journal paper details with quartile and IF',
    },
    {
      id: 'conferences',
      label: 'Conferences (per year)',
      description: 'Full conference paper details with indexing',
    },
  ];

  readonly exportIsValid = computed(
    () => this.exportYears().length > 0 && this.exportSheets().length > 0,
  );

  @ViewChild('trendCanvas') trendCanvas!: ElementRef<HTMLCanvasElement>;
  private trendChart: Chart | null = null;

  // --- derived --------------------------------------------------------
  readonly kpiCards = computed(() => {
    const o = this.overview();
    if (!o) return [];
    return [
      { label: 'Papers',      value: o.total_papers,      sub: 'all publications' },
      { label: 'Researchers', value: o.total_researchers, sub: 'active' },
      { label: 'Citations',   value: o.total_citations,   sub: 'total' },
      { label: 'Q1 Papers',   value: o.q1_papers,         sub: 'top quartile' },
    ];
  });

  readonly filteredResearchers = computed(() => {
    const did = this.selectedDepartmentId();
    const all = this.researchers();
    return did == null ? all : all.filter((r) => r.department_id === did);
  });

  // --- lifecycle ------------------------------------------------------
  ngOnInit(): void {
    this.refreshScopedData();
    // The trend chart always shows both years — it's a comparison.
    this.api.trends().subscribe({
      next: (d) => { this.trendData.set(d.results); this.renderTrendChart(); },
      error: () => this.loadFailed.set(true),
    });
  }

  // Reloads Overview/Departments/Researchers/KPIs for the current
  // globalYear — on init and on every year-pill click.
  private refreshScopedData(): void {
    const y = this.globalYear();
    // Clear first so the loading state shows.
    this.overview.set(null);
    this.departments.set([]);
    this.researchers.set([]);
    this.kpis.set(null);
    this.loadFailed.set(false);

    this.api.overview(y).subscribe({
      next: (d) => this.overview.set(d), error: () => this.loadFailed.set(true) });
    this.api.departments(y).subscribe({
      next: (d) => this.departments.set(d.results), error: () => this.loadFailed.set(true) });
    this.api.researchers(undefined, y).subscribe({
      next: (d) => this.researchers.set(d.results), error: () => this.loadFailed.set(true) });
    this.loadKpis();
  }

  setGlobalYear(value: number | 'all'): void {
    if (this.globalYear() === value) return;
    this.globalYear.set(value);
    this.refreshScopedData();
  }

  // ---- Export modal handlers ---------------------------------------
  openExportModal(): void {
    // Reset to all years/all sheets on open — remembering prior picks
    // tends to confuse more than it helps.
    this.exportYears.set([...this.availableYears]);
    this.exportSheets.set(
      this.exportSheetOptions.map((o) => o.id),
    );
    this.exportModalOpen.set(true);
  }

  closeExportModal(): void {
    this.exportModalOpen.set(false);
  }

  toggleExportYear(year: number): void {
    const current = this.exportYears();
    this.exportYears.set(
      current.includes(year)
        ? current.filter((y) => y !== year)
        : [...current, year],
    );
  }

  toggleExportSheet(id: string): void {
    const current = this.exportSheets();
    this.exportSheets.set(
      current.includes(id)
        ? current.filter((s) => s !== id)
        : [...current, id],
    );
  }

  downloadExport(): void {
    if (!this.exportIsValid()) return;
    const url = this.api.exportExcelUrl(this.exportYears(), this.exportSheets());
    // Programmatic <a> click so the download fires without navigating away.
    const a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    this.closeExportModal();
  }

  // KPIs are per-year, so when the filter is 'all' we anchor to the most
  // recent year (2026) for the freshest snapshot.
  private loadKpis(): void {
    const g = this.globalYear();
    const anchorYear = g === 'all' ? 2026 : g;
    this.api.kpis(anchorYear).subscribe({
      next: (data) => this.kpis.set(data),
      error: () => this.loadFailed.set(true),
    });
  }

  // KPI value -> 0-100% bar width, capped at `max` so outliers don't
  // squash everything else.
  kpiBarWidth(value: number, max: number): number {
    if (!value || value <= 0) return 0;
    return Math.min(100, (value / max) * 100);
  }

  ngAfterViewInit(): void {
    // The chart needs both the data (from ngOnInit) and a ready canvas;
    // this hook covers the canvas side.
    this.renderTrendChart();
  }

  // --- actions --------------------------------------------------------
  setDepartmentFilter(id: number | null): void {
    this.selectedDepartmentId.set(id);
  }

  filterByDepartment(id: number): void {
    this.selectedDepartmentId.set(id);
    // Scroll down to the researcher list.
    setTimeout(() => {
      window.scrollBy({ top: 600, behavior: 'smooth' });
    }, 50);
  }

  // --- chart ----------------------------------------------------------
  private renderTrendChart(): void {
    const data = this.trendData();
    if (!this.trendCanvas || data.length === 0) return;

    if (this.trendChart) {
      this.trendChart.destroy();
    }
    this.trendChart = new Chart(this.trendCanvas.nativeElement, {
      type: 'bar',
      data: {
        labels: data.map((d) => d.year.toString()),
        datasets: [
          {
            label: 'Publications',
            data: data.map((d) => d.papers),
            // Two indigo shades alternating across the bars.
            backgroundColor: ['rgba(99, 102, 241, 0.85)', 'rgba(129, 140, 248, 0.85)'],
            borderRadius: 8,
            // Wide bars read better for a two-bar comparison.
            barThickness: 64,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 1.2, // near-square to fit the compact card
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#111827',
            padding: 10,
            titleFont: { size: 12, weight: 'normal' },
            bodyFont: { size: 13, weight: 'bold' },
            callbacks: {
              label: (ctx) => `${(ctx.parsed.y ?? 0).toLocaleString()} publications`,
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { font: { size: 13, weight: 'bold' }, color: '#374151' },
          },
          y: {
            beginAtZero: true,
            grid: { color: '#f3f4f6' },
            ticks: { font: { size: 11 }, color: '#9ca3af', stepSize: 50 },
          },
        },
      },
    });
  }
}
