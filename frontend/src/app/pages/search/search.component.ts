/**
 * Search Page — dedicated /search route.
 *
 * Why a full page (vs the prior modal)?
 *   • More breathing room for the result cards.
 *   • Shareable URL (?q=...) so a search can be linked or bookmarked.
 *   • Browser back/forward behaves naturally.
 *
 * The permission gate stays on the BACKEND (/api/search/) — this page
 * just renders whatever the API returned and labels the scope when the
 * user is restricted to system-authored papers.
 */
import {
  Component, OnInit, OnDestroy, AfterViewInit,
  inject, signal, ViewChild, ElementRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import {
  Subject, debounceTime, distinctUntilChanged, switchMap, of, catchError,
} from 'rxjs';
import { LitrixApiService } from '../../services/litrix-api.service';
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

  readonly query    = signal<string>('');
  readonly loading  = signal<boolean>(false);
  readonly profiles = signal<SearchProfileResult[]>([]);
  readonly papers   = signal<SearchPaperResult[]>([]);
  readonly hasFullAccess = signal<boolean>(false);
  readonly hasSearched   = signal<boolean>(false);
  readonly error    = signal<string | null>(null);

  readonly selectedPaperId = signal<number | null>(null);

  private input$ = new Subject<string>();

  ngOnInit() {
    // Wire the debounced pipeline.
    // Pipeline survives backend errors via catchError inside switchMap —
    // the outer observable never errors, so the input keeps working on
    // the next keystroke without a manual re-subscribe dance.
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
          return this.api.search(q).pipe(
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
        this.hasFullAccess.set(!!res.has_full_access);
      });

    // Hydrate from ?q= so the page is shareable / refresh-safe.
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
    // Reflect the query in the URL so refresh / share works. Use
    // replaceUrl so we don't pollute history on every keystroke.
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
    this.hasSearched.set(false);
    this.router.navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl: true });
    this.searchInput?.nativeElement.focus();
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
    const src = p.full_name_ar || p.full_name_en || '';
    return src.split(/\s+/).slice(0, 2)
      .map(s => s[0] || '').join('').toUpperCase() || '?';
  }
}
