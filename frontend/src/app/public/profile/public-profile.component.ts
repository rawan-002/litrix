// Researcher drill-down reached from the dashboard list: bio header, KPI
// cards, a citations chart (2019+), and a papers list. Click a title for
// the detail modal.
import {
  Component, OnInit, signal, inject, ViewChild,
  ElementRef, AfterViewInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Chart, registerables } from 'chart.js';
import {
  PublicApiService, ResearcherProfile, PaperSummary, PaperDetailResponse,
} from '../services/public-api.service';

Chart.register(...registerables);

// Citations chart floor — pre-2019 years are near-zero here and just
// clutter the line.
const CHART_MIN_YEAR = 2019;

@Component({
  selector: 'app-public-profile',
  standalone: true,
  imports: [CommonModule, RouterLink],
  template: `
    <div class="min-h-screen bg-white">
      <!-- Back link -->
      <div class="max-w-5xl mx-auto px-6 pt-8">
        <a routerLink="/public/dashboard"
           class="inline-flex items-center text-sm text-gray-500 hover:text-indigo-600 transition-colors">
          <span class="mr-2">←</span> Back to dashboard
        </a>
      </div>

      <ng-container *ngIf="profile() as p">
        <!-- Bio header -->
        <header class="max-w-5xl mx-auto px-6 py-12 border-b border-gray-100 relative">
          <!-- Logo, top-right -->
          <img src="litrix-logo.png"
               alt="Litrix"
               class="absolute top-12 right-6 h-12 md:h-14 w-auto select-none"
               draggable="false" />
          <p class="text-sm uppercase tracking-widest text-indigo-600 font-medium">
            {{ p.department_name || 'College of Computing & IT' }}
          </p>
          <h1 class="mt-3 text-4xl md:text-5xl font-semibold text-gray-900 tracking-tight">
            {{ p.full_name_ar }}
          </h1>
          <p *ngIf="p.full_name_en && p.full_name_en !== p.full_name_ar"
             class="mt-2 text-lg text-gray-500">
            {{ p.full_name_en }}
          </p>

          <div class="mt-6 flex flex-wrap gap-3">
            <span *ngIf="p.academic_rank"
                  class="inline-flex items-center px-3 py-1 rounded-full bg-gray-100 text-sm text-gray-700">
              {{ p.academic_rank }}
            </span>
            <span *ngIf="p.specialization"
                  class="inline-flex items-center px-3 py-1 rounded-full bg-gray-100 text-sm text-gray-700">
              {{ p.specialization }}
            </span>
            <span class="inline-flex items-center px-3 py-1 rounded-full bg-indigo-50 text-sm text-indigo-700 font-mono">
              {{ p.litrix_id }}
            </span>
          </div>

          <div *ngIf="p.scopus_id || p.orcid_id" class="mt-4 flex flex-wrap gap-4 text-sm text-gray-500">
            <span *ngIf="p.scopus_id">
              <span class="text-gray-400">Scopus ID:</span>
              <span class="ml-1 font-mono text-gray-700">{{ p.scopus_id }}</span>
            </span>
            <span *ngIf="p.orcid_id">
              <span class="text-gray-400">ORCID:</span>
              <a [href]="'https://orcid.org/' + p.orcid_id" target="_blank"
                 class="ml-1 font-mono text-indigo-600 hover:underline">
                {{ p.orcid_id }}
              </a>
            </span>
          </div>
        </header>

        <!-- KPI cards -->
        <section class="max-w-5xl mx-auto px-6 py-12">
          <div class="grid grid-cols-2 md:grid-cols-4 gap-4 md:gap-6">
            <div class="rounded-2xl bg-gray-50 p-6">
              <p class="text-xs text-gray-500 uppercase tracking-wider font-medium">Papers</p>
              <p class="mt-3 text-4xl font-semibold text-gray-900 tracking-tight">{{ p.stats.total_papers | number }}</p>
            </div>
            <div class="rounded-2xl bg-gray-50 p-6">
              <p class="text-xs text-gray-500 uppercase tracking-wider font-medium">Citations</p>
              <p class="mt-3 text-4xl font-semibold text-gray-900 tracking-tight">{{ p.stats.total_citations | number }}</p>
            </div>
            <div class="rounded-2xl bg-gray-50 p-6">
              <p class="text-xs text-gray-500 uppercase tracking-wider font-medium">Q1 Papers</p>
              <p class="mt-3 text-4xl font-semibold text-indigo-600 tracking-tight">{{ p.stats.q1_papers | number }}</p>
            </div>
            <div class="rounded-2xl bg-gray-50 p-6">
              <p class="text-xs text-gray-500 uppercase tracking-wider font-medium">Active Years</p>
              <p class="mt-3 text-2xl md:text-3xl font-semibold text-gray-900 tracking-tight">
                {{ p.stats.first_year || '—' }}–{{ p.stats.last_year || '—' }}
              </p>
            </div>
          </div>
        </section>

        <!-- Citations chart (2019+) -->
        <section *ngIf="hasChartData()" class="max-w-5xl mx-auto px-6 py-4">
          <h2 class="text-xl font-semibold text-gray-900 mb-4">
            Citations by Year
            <span class="ml-2 text-sm font-normal text-gray-400">{{ CHART_MIN_YEAR }}+</span>
          </h2>
          <div class="rounded-2xl border border-gray-100 p-5 max-w-2xl">
            <canvas #citationsCanvas></canvas>
          </div>
        </section>

        <!-- Papers list -->
        <section class="max-w-5xl mx-auto px-6 py-12">
          <h2 class="text-2xl font-semibold text-gray-900 mb-6">
            Publications
            <span class="ml-2 text-base font-normal text-gray-400">
              ({{ p.papers.length | number }})
            </span>
          </h2>

          <div class="space-y-3">
            <article *ngFor="let paper of p.papers"
                     class="rounded-2xl border border-gray-100 p-6 hover:shadow-md hover:border-indigo-100 transition-all">
              <div class="flex items-start justify-between gap-4">
                <div class="flex-1 min-w-0">
                  <h3 (click)="openPaperDetail(paper)"
                      class="font-medium text-gray-900 leading-snug cursor-pointer hover:text-indigo-600 transition-colors">
                    {{ paper.title }}
                  </h3>
                  <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-500">
                    <span *ngIf="paper.pub_year" class="font-medium">{{ paper.pub_year }}</span>
                    <span *ngIf="paper.journal_name" class="text-gray-400">·</span>
                    <span *ngIf="paper.journal_name">{{ paper.journal_name }}</span>
                    <span *ngIf="paper.quartile"
                          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold"
                          [ngClass]="quartileClass(paper.quartile)">
                      {{ paper.quartile }}
                    </span>
                  </div>
                </div>
                <div class="text-right flex-shrink-0">
                  <p class="text-3xl font-semibold text-gray-900 tracking-tight">{{ paper.citations | number }}</p>
                  <p class="text-xs text-gray-400 uppercase tracking-wider">Citations</p>
                </div>
              </div>
            </article>
          </div>

          <div *ngIf="p.papers.length === 0" class="text-center py-12 text-gray-400">
            No publications found.
          </div>
        </section>
      </ng-container>

      <!-- Loading state -->
      <div *ngIf="!profile() && !errorMsg()" class="max-w-5xl mx-auto px-6 py-32 text-center">
        <p class="text-gray-400">Loading…</p>
      </div>

      <!-- Error state -->
      <div *ngIf="errorMsg() as err" class="max-w-5xl mx-auto px-6 py-32 text-center">
        <p class="text-gray-500">{{ err }}</p>
        <a routerLink="/public/dashboard"
           class="mt-4 inline-block text-indigo-600 hover:underline">
          ← Back to dashboard
        </a>
      </div>

      <!-- Footer -->
      <footer class="border-t border-gray-100 mt-16">
        <div class="max-w-7xl mx-auto px-6 py-8 text-center text-sm text-gray-400">
          Litrix Research Analytics · College of Computing & IT · Al-Baha University
        </div>
      </footer>

      <!-- Paper detail modal -->
      <div *ngIf="selectedPaper() as detail"
           class="fixed inset-0 z-50 flex items-center justify-center px-4 py-8"
           (click)="closePaperDetail()">
        <div class="absolute inset-0 bg-gray-900/60 backdrop-blur-sm"></div>

        <div class="relative bg-white rounded-3xl shadow-2xl max-w-3xl w-full max-h-[85vh] overflow-y-auto"
             (click)="$event.stopPropagation()">
          <button (click)="closePaperDetail()"
                  class="absolute top-4 right-4 w-9 h-9 rounded-full bg-gray-100 hover:bg-gray-200 flex items-center justify-center text-gray-500 transition-colors"
                  aria-label="Close">
            ✕
          </button>

          <div class="p-8 md:p-10">
            <!-- Quartile + Year header -->
            <div class="flex items-center gap-3 mb-3">
              <span *ngIf="detail.quartile"
                    class="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold"
                    [ngClass]="quartileClass(detail.quartile)">
                {{ detail.quartile }}
              </span>
              <span *ngIf="detail.pub_year" class="text-sm text-gray-500 font-medium">
                {{ detail.pub_year }}
              </span>
            </div>

            <!-- Title -->
            <h2 class="text-2xl md:text-3xl font-semibold text-gray-900 leading-tight pr-8">
              {{ detail.title }}
            </h2>

            <!-- Journal info -->
            <div *ngIf="detail.journal_name" class="mt-4 text-gray-700">
              <p class="text-base">{{ detail.journal_name }}</p>
              <p *ngIf="detail.publisher" class="text-sm text-gray-400 mt-0.5">{{ detail.publisher }}</p>
            </div>

            <!-- Stats row -->
            <div class="mt-6 grid grid-cols-3 gap-4">
              <div class="rounded-xl bg-gray-50 p-4 text-center">
                <p class="text-2xl font-semibold text-gray-900">{{ detail.citations | number }}</p>
                <p class="text-xs text-gray-500 uppercase tracking-wider mt-1">Citations</p>
              </div>
              <div class="rounded-xl bg-gray-50 p-4 text-center">
                <p class="text-2xl font-semibold text-gray-900">
                  {{ detail.impact_factor != null ? (detail.impact_factor | number:'1.1-2') : '—' }}
                </p>
                <p class="text-xs text-gray-500 uppercase tracking-wider mt-1">Impact Factor</p>
              </div>
              <div class="rounded-xl bg-gray-50 p-4 text-center">
                <p class="text-2xl font-semibold text-gray-900">
                  {{ (detail.internal_authors.length + detail.external_authors.length) }}
                </p>
                <p class="text-xs text-gray-500 uppercase tracking-wider mt-1">Authors</p>
              </div>
            </div>

            <!-- Real prose only — skip the Scopus-URL placeholder -->
            <div *ngIf="hasRealAbstract(detail)" class="mt-8">
              <h3 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-2">Abstract</h3>
              <p class="text-gray-700 leading-relaxed">{{ detail.abstract }}</p>
            </div>

            <!-- Conference papers with no abstract — link out to Scopus -->
            <div *ngIf="!hasRealAbstract(detail) && abstractIsUrl(detail.abstract)" class="mt-8">
              <h3 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-2">Abstract</h3>
              <p class="text-gray-500 text-sm italic mb-2">
                Abstract not provided. View the full record on Scopus:
              </p>
              <a [href]="detail.abstract" target="_blank"
                 class="inline-flex items-center text-indigo-600 hover:underline text-sm font-medium break-all">
                Open Scopus record ↗
              </a>
            </div>

            <!-- Authors -->
            <div *ngIf="detail.internal_authors.length || detail.external_authors.length" class="mt-8">
              <h3 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">Authors</h3>
              <div class="flex flex-wrap gap-2">
                <a *ngFor="let a of detail.internal_authors"
                   [routerLink]="['/public/researcher', a.litrix_id]"
                   class="inline-flex items-center px-3 py-1.5 rounded-full bg-indigo-50 text-sm text-indigo-700 hover:bg-indigo-100 transition-colors">
                  {{ a.name }}
                  <span *ngIf="a.is_corresponding" class="ml-1 text-xs text-indigo-500">✉</span>
                </a>
                <span *ngFor="let a of detail.external_authors"
                      class="inline-flex items-center px-3 py-1.5 rounded-full bg-gray-100 text-sm text-gray-600">
                  {{ a.name }}
                </span>
              </div>
            </div>

            <!-- Publication metadata -->
            <div *ngIf="detail.doi || detail.volume || detail.issue || detail.pages"
                 class="mt-8 pt-6 border-t border-gray-100">
              <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                <div *ngIf="detail.volume">
                  <p class="text-xs text-gray-400 uppercase tracking-wider">Volume</p>
                  <p class="text-gray-700 mt-0.5">{{ detail.volume }}</p>
                </div>
                <div *ngIf="detail.issue">
                  <p class="text-xs text-gray-400 uppercase tracking-wider">Issue</p>
                  <p class="text-gray-700 mt-0.5">{{ detail.issue }}</p>
                </div>
                <div *ngIf="detail.pages">
                  <p class="text-xs text-gray-400 uppercase tracking-wider">Pages</p>
                  <p class="text-gray-700 mt-0.5">{{ detail.pages }}</p>
                </div>
                <div *ngIf="detail.doi" class="col-span-2 md:col-span-1">
                  <p class="text-xs text-gray-400 uppercase tracking-wider">DOI</p>
                  <a [href]="'https://doi.org/' + detail.doi" target="_blank"
                     class="text-indigo-600 hover:underline mt-0.5 block break-all">
                    {{ detail.doi }}
                  </a>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class PublicProfileComponent implements OnInit, AfterViewInit {
  private readonly api = inject(PublicApiService);
  private readonly route = inject(ActivatedRoute);

  readonly CHART_MIN_YEAR = CHART_MIN_YEAR;

  readonly profile = signal<ResearcherProfile | null>(null);
  readonly errorMsg = signal<string | null>(null);
  readonly selectedPaper = signal<PaperDetailResponse | null>(null);

  @ViewChild('citationsCanvas') citationsCanvas!: ElementRef<HTMLCanvasElement>;
  private citationsChart: Chart | null = null;

  ngOnInit(): void {
    const litrixId = this.route.snapshot.paramMap.get('litrixId') || '';
    if (!litrixId) {
      this.errorMsg.set('Missing researcher ID in URL.');
      return;
    }
    this.api.researcherProfile(litrixId).subscribe({
      next: (p) => {
        this.profile.set(p);
        setTimeout(() => this.renderCitationsChart(), 0);
      },
      error: (err) => {
        this.errorMsg.set(
          err?.status === 404
            ? `Researcher "${litrixId}" not found.`
            : 'Could not load profile. Please try again.',
        );
      },
    });
  }

  ngAfterViewInit(): void {
    this.renderCitationsChart();
  }

  hasChartData(): boolean {
    const p = this.profile();
    if (!p) return false;
    return p.citations_by_year_chart.some((d) => d.year >= CHART_MIN_YEAR);
  }

  openPaperDetail(paper: PaperSummary): void {
    this.api.paperDetail(paper.paper_id).subscribe({
      next: (detail) => this.selectedPaper.set(detail),
      error: () => {
        // Fall back to the list data we already have rather than erroring
        // on a non-critical click.
        this.selectedPaper.set({
          paper_id: paper.paper_id,
          title: paper.title,
          title_en: null, abstract: null, doi: paper.doi,
          pub_year: paper.pub_year, volume: null, issue: null, pages: null,
          language: null, source: paper.source,
          citations_by_year: paper.citations_by_year ?? null,
          citations: paper.citations,
          journal_name: paper.journal_name, publisher: null,
          quartile: paper.quartile,
          impact_factor: paper.impact_factor ?? null,
          category: null,
          internal_authors: [], external_authors: [],
        });
      },
    });
  }

  closePaperDetail(): void {
    this.selectedPaper.set(null);
  }

  // True only when the abstract is real prose. Scopus sometimes drops a
  // bare URL into the abstract column for conference papers, so we treat
  // anything under 80 chars or URL-shaped as "no abstract".
  hasRealAbstract(detail: PaperDetailResponse): boolean {
    const a = detail.abstract;
    if (!a) return false;
    const trimmed = a.trim();
    if (trimmed.length < 80) return false;
    if (this.abstractIsUrl(trimmed)) return false;
    return true;
  }

  abstractIsUrl(a: string | null): boolean {
    if (!a) return false;
    const t = a.trim();
    // A bare URL: http(s):// with no internal whitespace.
    return /^https?:\/\/\S+$/i.test(t);
  }

  quartileClass(q: string | null): string {
    switch (q) {
      case 'Q1': return 'bg-emerald-100 text-emerald-700';
      case 'Q2': return 'bg-blue-100 text-blue-700';
      case 'Q3': return 'bg-amber-100 text-amber-700';
      case 'Q4': return 'bg-gray-200 text-gray-700';
      default:   return 'bg-gray-100 text-gray-500';
    }
  }

  private renderCitationsChart(): void {
    const p = this.profile();
    if (!p || !this.citationsCanvas) return;
    const data = p.citations_by_year_chart.filter((d) => d.year >= CHART_MIN_YEAR);
    if (data.length === 0) return;

    if (this.citationsChart) this.citationsChart.destroy();
    this.citationsChart = new Chart(this.citationsCanvas.nativeElement, {
      type: 'line',
      data: {
        labels: data.map((d) => d.year.toString()),
        datasets: [
          {
            label: 'Citations',
            data: data.map((d) => d.citations),
            borderColor: 'rgba(99, 102, 241, 1)',
            backgroundColor: 'rgba(99, 102, 241, 0.08)',
            fill: true,
            tension: 0.4,
            pointRadius: 3,
            pointHoverRadius: 5,
            pointBackgroundColor: 'rgba(99, 102, 241, 1)',
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 2.2,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#111827', padding: 10,
            titleFont: { size: 12, weight: 'normal' },
            bodyFont: { size: 13, weight: 'bold' },
            callbacks: { label: (ctx) => `${ctx.parsed.y ?? 0} citations` },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { font: { size: 11 } } },
          y: {
            beginAtZero: true,
            grid: { color: '#f3f4f6' },
            ticks: { font: { size: 11 }, color: '#9ca3af' },
          },
        },
      },
    });
  }
}
