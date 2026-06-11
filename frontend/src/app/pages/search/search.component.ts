// Full-page /search (replaced the old modal) so results have room and the
// ?q= URL is shareable. The permission gate lives on the backend — this page
// just renders what /api/search/ returns and labels the scope when the user
// is limited to system-authored papers.
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
    // catchError lives inside switchMap so a backend error never kills the
    // outer stream — the input keeps working on the next keystroke.
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
