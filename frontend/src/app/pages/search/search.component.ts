// Full-page /search (replaced the old modal) so results have room and the
// ?q= URL is shareable. The permission gate lives on the backend - this page
// just renders what /api/search/ returns and labels the scope when the user
// is limited to system-authored papers.
import {
  Component, OnInit, OnDestroy, AfterViewInit,
  inject, signal, effect, untracked, ViewChild, ElementRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import {
  Subject, debounceTime, distinctUntilChanged, switchMap, of, catchError,
} from 'rxjs';
import { LitrixApiService } from '../../services/litrix-api.service';
import { AffiliationService } from '../../core/services/affiliation.service';
import { researcherPrimaryName } from '../../shared/utils/researcher-name';
import {
  SearchProfileResult, SearchPaperResult,
} from '../../models/litrix.models';
import { PaperDetailModalComponent } from
  '../../components/paper-detail-modal/paper-detail-modal.component';


@Component({
  selector: 'app-search',
  standalone: true,
  imports: [CommonModule, FormsModule, PaperDetailModalComponent],
  templateUrl: './search.component.html',
})
export class SearchComponent implements OnInit, AfterViewInit, OnDestroy {
  @ViewChild('searchInput') searchInput?: ElementRef<HTMLInputElement>;

  private readonly api = inject(LitrixApiService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly affiliation = inject(AffiliationService);
  readonly albahaOnly = this.affiliation.albahaOnly;

  readonly PAGE = 10;

  // Top-level result tabs. Publications = Journal + Conference, Books =
  // Book + Book Chapter, Researchers = profiles. Kept as one /api/search/
  // call (not three separate endpoints) - the backend's venue_type param
  // takes a comma list, so switching tabs just changes which types we ask
  // for; profiles come back regardless and are simply not shown outside the
  // Researchers tab.
  readonly tabs = [
    { key: 'publications' as const, label: 'Publications' },
    { key: 'books'        as const, label: 'Books' },
    { key: 'researchers'  as const, label: 'Researchers' },
  ];
  readonly activeTab = signal<'publications' | 'books' | 'researchers'>('publications');

  // Sub-filter within the Publications tab.
  readonly pubTypes = [
    { key: 'all'        as const, label: 'All' },
    { key: 'Journal'    as const, label: 'Journal' },
    { key: 'Conference' as const, label: 'Conference' },
  ];
  readonly pubType = signal<'all' | 'Journal' | 'Conference'>('all');

  // Sub-filter within the Books tab.
  readonly bookTypes = [
    { key: 'all'         as const, label: 'All' },
    { key: 'Book'        as const, label: 'Book' },
    { key: 'BookChapter' as const, label: 'Book Chapter' },
  ];
  readonly bookType = signal<'all' | 'Book' | 'BookChapter'>('all');

  /** venue_type param actually sent to the API for the active tab. */
  private venueTypeParam(): string | undefined {
    if (this.activeTab() === 'books') {
      return this.bookType() === 'all' ? 'Book,BookChapter' : this.bookType();
    }
    if (this.activeTab() === 'publications') {
      return this.pubType() === 'all' ? 'Journal,Conference' : this.pubType();
    }
    return undefined;
  }

  setTab(t: 'publications' | 'books' | 'researchers') {
    this.activeTab.set(t);
  }
  setPubType(v: 'all' | 'Journal' | 'Conference') {
    this.pubType.set(v);
  }
  setBookType(v: 'all' | 'Book' | 'BookChapter') {
    this.bookType.set(v);
  }

  /** Small emoji marker so Journal/Conference/Book/Book Chapter read apart
   *  at a glance in a mixed results list. */
  venueIcon(vt: string | null | undefined): string {
    switch ((vt || '').toLowerCase()) {
      case 'journal':      return '📄';
      case 'book':         return '📘';
      case 'bookchapter':  return '📖';
      default:             return vt?.toLowerCase().startsWith('conf') ? '🎤' : '';
    }
  }

  readonly query    = signal<string>('');
  readonly loading  = signal<boolean>(false);
  readonly loadingMore = signal<boolean>(false);
  readonly profiles = signal<SearchProfileResult[]>([]);
  readonly papers   = signal<SearchPaperResult[]>([]);
  readonly papersHasMore = signal<boolean>(false);
  readonly paperLimit    = signal<number>(this.PAGE);
  readonly hasFullAccess = signal<boolean>(false);
  readonly hasSearched   = signal<boolean>(false);
  readonly error    = signal<string | null>(null);

  readonly selectedPaperId = signal<number | null>(null);

  private input$ = new Subject<string>();

  constructor() {
    // Header's Al-Baha toggle, the active tab, and each tab's own sub-filter
    // all re-run the current query through the same pipeline as typing does,
    // so results refresh live without retyping. Guarded to a real query
    // (empty at construction time, before ngOnInit even subscribes to
    // input$) so this never fires a spurious search.
    effect(() => {
      this.affiliation.albahaOnly();
      this.activeTab();
      this.pubType();
      this.bookType();
      untracked(() => {
        const q = this.query().trim();
        if (q.length >= 2) this.input$.next(q);
      });
    });
  }

  ngOnInit() {
    // catchError lives inside switchMap so a backend error never kills the
    // outer stream - the input keeps working on the next keystroke.
    this.input$
      .pipe(
        debounceTime(250),
        distinctUntilChanged(),
        switchMap(q => {
          if (q.length < 2) {
            this.profiles.set([]);
            this.papers.set([]);
            this.hasSearched.set(false);
            this.error.set(null);
            return of(null);
          }
          this.loading.set(true);
          this.hasSearched.set(true);
          this.error.set(null);
          // Every fresh query restarts paging from the first page.
          this.paperLimit.set(this.PAGE);
          return this.api.search(q, this.PAGE, this.albahaOnly(), this.venueTypeParam()).pipe(
            catchError(err => {
              this.error.set(
                err?.error?.error ||
                err?.message ||
                `Search failed (HTTP ${err?.status ?? '?'})`,
              );
              this.profiles.set([]);
              this.papers.set([]);
              return of(null);
            }),
          );
        }),
      )
      .subscribe(res => {
        this.loading.set(false);
        if (!res) return;
        this.profiles.set(res.profiles ?? []);
        this.papers.set(res.papers ?? []);
        this.papersHasMore.set(!!res.papers_has_more);
        this.hasFullAccess.set(!!res.has_full_access);
      });

    // Hydrate from ?q= so refresh and shared links work.
    const initial = this.route.snapshot.queryParamMap.get('q') ?? '';
    if (initial) {
      this.query.set(initial);
      this.input$.next(initial.trim());
    }
  }

  ngAfterViewInit() {
    queueMicrotask(() => this.searchInput?.nativeElement.focus());
  }

  ngOnDestroy() {
    this.input$.complete();
  }

  onInput(value: string) {
    this.query.set(value);
    this.input$.next(value.trim());
    // Mirror the query into the URL, replaceUrl so every keystroke doesn't
    // pile up in history.
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: value ? { q: value } : {},
      replaceUrl: true,
    });
  }

  clear() {
    this.query.set('');
    this.profiles.set([]);
    this.papers.set([]);
    this.papersHasMore.set(false);
    this.paperLimit.set(this.PAGE);
    this.hasSearched.set(false);
    this.router.navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl: true });
    this.searchInput?.nativeElement.focus();
  }

  // Re-fetch the same query with a larger page size. The backend returns
  // results from the top, so we just replace the list with the bigger page.
  loadMorePapers() {
    const q = this.query().trim();
    if (q.length < 2 || this.loadingMore()) return;
    const next = this.paperLimit() + this.PAGE;
    this.paperLimit.set(next);
    this.loadingMore.set(true);
    this.api.search(q, next, this.albahaOnly(), this.venueTypeParam()).pipe(
      catchError(() => of(null)),
    ).subscribe(res => {
      this.loadingMore.set(false);
      if (!res) return;
      this.papers.set(res.papers ?? []);
      this.papersHasMore.set(!!res.papers_has_more);
    });
  }

  openProfile(p: SearchProfileResult) {
    this.router.navigate(['/profile', p.litrix_id]);
  }

  openPaper(p: SearchPaperResult) {
    this.selectedPaperId.set(p.paper_id);
  }

  closePaper() {
    this.selectedPaperId.set(null);
  }

  initials(p: SearchProfileResult): string {
    const src = researcherPrimaryName(p);
    return src.split(/\s+/).slice(0, 2)
      .map(s => s[0] || '').join('').toUpperCase() || '?';
  }

  primaryName(p: SearchProfileResult): string {
    return researcherPrimaryName(p, 'Unnamed');
  }
}
