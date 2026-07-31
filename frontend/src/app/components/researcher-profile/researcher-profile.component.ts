// Researcher profile page: identity header, KPI cards, citations chart, and
// the full papers list with sort/filter. Reads the route id and fetches the
// profile in one round-trip.
import { Component, OnInit, Input, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';
import { AffiliationService } from '../../core/services/affiliation.service';
import {
  ResearcherProfilePayload, ProfilePaper,
} from '../../models/litrix.models';
import { PaperDetailModalComponent } from
  '../paper-detail-modal/paper-detail-modal.component';
import { CitationsChartComponent } from
  '../citations-chart/citations-chart.component';
import { IconComponent } from '../../shared/icon/icon.component';
import { isLatinName, joinEnglishName } from '../../shared/utils/researcher-name';

@Component({
  selector: 'app-researcher-profile',
  standalone: true,
  imports: [
    CommonModule, RouterLink, PaperDetailModalComponent, CitationsChartComponent,
    IconComponent,
  ],
  templateUrl: './researcher-profile.component.html',
})
export class ResearcherProfileComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  private readonly affiliation = inject(AffiliationService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly data    = signal<ResearcherProfilePayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  // Paper detail modal - store paper_id; the shared modal fetches full data
  readonly selectedPaperId = signal<number | null>(null);

  // Pagination
  readonly LOAD_BATCH = 15;
  readonly visibleCount = signal<number>(15);

  // Sorting controls (Google Scholar-style toggleable arrows)
  readonly sortField = signal<'year' | 'citations'>('year');
  readonly sortDir   = signal<'asc' | 'desc'>('desc');

  // Quartile filter - multi-select. Empty Set = no filter (show all).
  readonly activeQuartiles = signal<Set<string>>(new Set());

  // Journals vs Conferences. Same rule as the dashboard/export: a venue
  // starting with "conf" is a conference, everything else is a journal.
  readonly activeVenue = signal<'all' | 'journal' | 'conference'>('all');
  readonly venueTabs = [
    { key: 'all'        as const, label: 'All' },
    { key: 'journal'    as const, label: 'Journals' },
    { key: 'conference' as const, label: 'Conferences' },
  ];

  private isConference(p: ProfilePaper): boolean {
    return (p.venue_type || '').toLowerCase().startsWith('conf');
  }

  /** Paper counts per venue tab (respecting the affiliation filter). */
  readonly venueCounts = computed(() => {
    let papers = this.data()?.papers ?? [];
    if (this.effectiveAlbahaOnly()) {
      papers = papers.filter(p => this.isAlbahaFor(p));
    }
    let conf = 0;
    for (const p of papers) if (this.isConference(p)) conf++;
    return { all: papers.length, conference: conf, journal: papers.length - conf };
  });

  setVenue(v: 'all' | 'journal' | 'conference') {
    this.activeVenue.set(v);
    this.visibleCount.set(this.LOAD_BATCH);
  }

  // Mirrors the platform-wide Al-Baha switch (the header toggle). 'albaha'
  // keeps only papers confirmed Al-Baha (affiliation_verified === true);
  // confirmed-foreign and still-unverified papers are both hidden. Drives BOTH
  // the headline KPIs and the papers list, so they always agree.
  readonly affiliationFilter = computed<'albaha' | 'all'>(() =>
    this.affiliation.albahaOnly() ? 'albaha' : 'all'
  );

  // TEST feature (Moez Krichen's profile only, see below): when active,
  // this profile's Al-Baha filter runs off a freshly-fetched live OpenAlex
  // per-paper check instead of the stored affiliation_verified column - same
  // filtering pipeline as the header toggle, different data source. Remove
  // this block (and isAlbahaFor's branch) once the trial is done.
  readonly openAlexLiveActive = signal(false);
  readonly effectiveAlbahaOnly = computed(() =>
    this.openAlexLiveActive() || this.affiliationFilter() === 'albaha'
  );
  private isAlbahaFor(p: ProfilePaper): boolean {
    if (this.openAlexLiveActive()) {
      return this.openAlexLiveMap()?.get(p.paper_id) === true;
    }
    return p.affiliation_verified === true;
  }

  // The papers the KPIs are computed over - same Al-Baha filter as the list.
  readonly statsPapers = computed(() => {
    const all = this.data()?.papers ?? [];
    return this.effectiveAlbahaOnly()
      ? all.filter(p => this.isAlbahaFor(p))
      : all;
  });

  readonly kpiPublications = computed(() => this.statsPapers().length);
  readonly kpiCitations = computed(() =>
    this.statsPapers().reduce((s, p) => s + (p.citations ?? 0), 0)
  );
  readonly kpiQ1 = computed(() =>
    this.statsPapers().filter(p => p.quartile === 'Q1').length
  );

  /** Papers hidden by the "Al-Baha only" view (confirmed foreign + unverified). */
  readonly hiddenCount = computed(() =>
    (this.data()?.papers ?? []).filter(p => !this.isAlbahaFor(p)).length
  );

  // Clip the citations chart before this year. The profile chart starts at
  // 2021 (earlier years are near-zero and just stretch the axis).
  readonly CHART_YEAR_FLOOR = 2021;

  // The citations-over-time series the chart actually draws. When the Al-Baha
  // filter is OFF, use Scholar's authoritative author-level graph (matches the
  // Scholar profile). When it's ON, that graph can't be split by affiliation,
  // so rebuild the series by summing the per-year citation counts of the
  // Al-Baha-only papers - so the chart reacts to the toggle like the KPIs do.
  readonly chartCitations = computed(() => {
    if (!this.effectiveAlbahaOnly()) {
      return this.data()?.citations_by_year ?? [];
    }
    const byYear = new Map<number, number>();
    for (const p of this.statsPapers()) {
      const cby = p.citations_by_year;
      if (!cby) continue;
      for (const [y, c] of Object.entries(cby)) {
        const yr = parseInt(y, 10);
        if (!Number.isFinite(yr) || yr < this.CHART_YEAR_FLOOR) continue;
        byYear.set(yr, (byYear.get(yr) ?? 0) + (Number(c) || 0));
      }
    }
    return [...byYear.entries()]
      .map(([year, citations]) => ({ year, citations }))
      .sort((a, b) => a.year - b.year);
  });

  // Web-of-Science-style derived metrics. The profile is lifetime by design,
  // so these run off the per-paper lifetime citation counts.
  readonly hIndex = computed(() => {
    const cites = this.statsPapers()
      .map(p => p.citations ?? 0)
      .sort((a, b) => b - a);
    let h = 0;
    for (let i = 0; i < cites.length; i++) {
      if (cites[i] >= i + 1) h = i + 1; else break;
    }
    return h;
  });

  readonly avgCitations = computed(() => {
    const papers = this.data()?.papers ?? [];
    if (!papers.length) return 0;
    const total = papers.reduce((s, p) => s + (p.citations ?? 0), 0);
    return Math.round((total / papers.length) * 10) / 10;
  });

  // Display name + initials for the identity banner: the exact name Google
  // Scholar shows on this researcher's profile. The Arabic name is never
  // shown here, not even as a fallback.
  readonly displayName = computed(() => {
    const id = this.data()?.identity;
    if (!id) return '';
    const fallbackEn = joinEnglishName(id.first_name, id.last_name);
    const en = id.scholar_display_name || fallbackEn;
    return (isLatinName(en) ? en : '') || 'Researcher';
  });

  readonly initials = computed(() => {
    const name = this.displayName();
    const parts = name.split(/\s+/).filter(Boolean).slice(0, 2);
    return parts.map(p => p[0] || '').join('').toUpperCase() || '?';
  });

  // Flips to true if the Scholar photo fails to load, so we fall back to initials.
  readonly avatarError = signal<boolean>(false);

  // TEST feature: an Al-Baha filter toggle sourced from a live per-paper
  // OpenAlex check instead of the stored affiliation_verified column -
  // Moez Krichen's profile only (Lit-000089), a one-off trial before
  // deciding whether to build this for every researcher. `openAlexLiveActive`
  // and `isAlbahaFor` above are the other half of this block. Remove all of
  // it (and the matching template button) once the trial is done.
  readonly TEST_OPENALEX_LITRIX_ID = 'Lit-000089';
  readonly showOpenAlexTestButton = computed(() =>
    this.data()?.identity?.litrix_id === this.TEST_OPENALEX_LITRIX_ID
  );
  readonly openAlexLiveMap = signal<Map<number, boolean | null> | null>(null);
  readonly openAlexLiveLoading = signal(false);
  readonly openAlexLiveError = signal<string | null>(null);

  // Toggle button: first click fetches (live, uncached) and turns the filter
  // on; a second click just turns it back off - re-clicking after that
  // reuses the already-fetched map rather than re-querying OpenAlex.
  toggleOpenAlexLiveTest() {
    if (this.openAlexLiveActive()) {
      this.openAlexLiveActive.set(false);
      return;
    }
    if (this.openAlexLiveMap()) {
      this.openAlexLiveActive.set(true);
      return;
    }
    const litrixId = this.data()?.identity?.litrix_id;
    if (!litrixId) return;
    this.openAlexLiveLoading.set(true);
    this.openAlexLiveError.set(null);
    this.api.getOpenAlexLiveAffiliationCheck(litrixId).subscribe({
      next: res => {
        const map = new Map<number, boolean | null>(
          Object.entries(res.results).map(([pid, v]) => [Number(pid), v])
        );
        this.openAlexLiveMap.set(map);
        this.openAlexLiveActive.set(true);
        this.openAlexLiveLoading.set(false);
      },
      error: err => {
        this.openAlexLiveError.set(`Failed: ${err.message}`);
        this.openAlexLiveLoading.set(false);
      },
    });
  }

  // Co-authors: show a first batch, reveal the rest on demand.
  readonly COAUTHOR_BATCH = 6;
  readonly coauthorsShown = signal<number>(6);

  // Co-authors also follow the Al-Baha switch: ON shows only Al-Baha
  // co-authors, OFF shows everyone (including the external Scholar names).
  readonly filteredCoauthors = computed(() => {
    const all = this.data()?.coauthors ?? [];
    return this.affiliationFilter() === 'albaha'
      ? all.filter(c => c.is_albaha)
      : all;
  });

  readonly visibleCoauthors = computed(() =>
    this.filteredCoauthors().slice(0, this.coauthorsShown())
  );

  showMoreCoauthors() {
    this.coauthorsShown.update(n => n + this.COAUTHOR_BATCH);
  }

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

  // Apply the affiliation/venue/quartile filters, then sort.
  readonly sortedPapers = computed(() => {
    let all = [...(this.data()?.papers ?? [])];

    if (this.effectiveAlbahaOnly()) {
      all = all.filter(p => this.isAlbahaFor(p));
    }

    const venue = this.activeVenue();
    if (venue === 'journal') {
      all = all.filter(p => !this.isConference(p));
    } else if (venue === 'conference') {
      all = all.filter(p => this.isConference(p));
    }

    // Empty Set = no quartile filter.
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

  // Bar-chart geometry from citations_by_year, clipped to CHART_YEAR_FLOOR.
  readonly chart = computed(() => {
    const cby = (this.data()?.citations_by_year ?? [])
      .filter(c => c.year >= this.CHART_YEAR_FLOOR);
    if (!cby.length) return null;
    const max = Math.max(...cby.map(c => c.citations), 1);
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

  /** Litrix-ID to display when this is hosted inside another page; otherwise
   *  falls back to the :litrixId route param. */
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
        // Some views return CitationsByYear as a JSON string rather than an
        // object - parse it defensively so the keyvalue pipe never crashes.
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
        this.coauthorsShown.set(this.COAUTHOR_BATCH);
        this.avatarError.set(false);
        this.loading.set(false);

        // The backend lookup is case/padding-insensitive, so the user may
        // arrive via /profile/LIT-0001 or /profile/lit-1. Silently rewrite
        // the URL to the canonical Lit-000001 (replaceUrl, no history entry).
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
    if (n == null) return '-';
    return n.toLocaleString('en-US');
  }

  citationsForYear(p: ProfilePaper, year: number): number {
    if (!p.citations_by_year) return 0;
    const v = p.citations_by_year[String(year)];
    return v || 0;
  }
}
