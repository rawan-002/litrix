// Spotlight-style global search modal (opened from the sidebar or Cmd/Ctrl+K).
// Debounced query, two result sections (profiles + papers). The permission
// gate lives on the backend; this component just renders what the API returns.
import {
  Component, EventEmitter, Output, OnInit, OnDestroy,
  inject, signal, ViewChild, ElementRef, AfterViewInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { Subject, debounceTime, distinctUntilChanged, switchMap, of } from 'rxjs';
import { LitrixApiService } from '../../services/litrix-api.service';
import {
  SearchProfileResult, SearchPaperResult,
} from '../../models/litrix.models';
import { PaperDetailModalComponent } from
  '../paper-detail-modal/paper-detail-modal.component';

@Component({
  selector: 'app-search-modal',
  standalone: true,
  imports: [CommonModule, FormsModule, PaperDetailModalComponent],
  templateUrl: './search-modal.component.html',
})
export class SearchModalComponent implements OnInit, AfterViewInit, OnDestroy {
  @Output() closed = new EventEmitter<void>();
  @ViewChild('searchInput') searchInput?: ElementRef<HTMLInputElement>;

  private readonly api = inject(LitrixApiService);
  private readonly router = inject(Router);

  readonly query    = signal<string>('');
  readonly loading  = signal<boolean>(false);
  readonly profiles = signal<SearchProfileResult[]>([]);
  readonly papers   = signal<SearchPaperResult[]>([]);
  readonly hasFullAccess = signal<boolean>(false);

  // Selected paper for the detail modal (lazy-fetched).
  readonly selectedPaperId = signal<number | null>(null);

  private input$ = new Subject<string>();

  ngOnInit() {
    this.input$
      .pipe(
        debounceTime(250),
        distinctUntilChanged(),
        switchMap(q => {
          if (q.length < 2) {
            this.profiles.set([]);
            this.papers.set([]);
            return of(null);
          }
          this.loading.set(true);
          return this.api.search(q);
        }),
      )
      .subscribe({
        next: res => {
          this.loading.set(false);
          if (!res) return;
          this.profiles.set(res.profiles);
          this.papers.set(res.papers);
          this.hasFullAccess.set(res.has_full_access);
        },
        error: () => this.loading.set(false),
      });
  }

  ngAfterViewInit() {
    // Auto-focus the input as soon as the modal mounts.
    queueMicrotask(() => this.searchInput?.nativeElement.focus());
  }

  ngOnDestroy() {
    this.input$.complete();
  }

  onInput(value: string) {
    this.query.set(value);
    this.input$.next(value.trim());
  }

  onKeyDown(ev: KeyboardEvent) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      this.close();
    }
  }

  close() {
    this.closed.emit();
  }

  openProfile(p: SearchProfileResult) {
    this.router.navigate(['/profile', p.litrix_id]);
    this.close();
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
