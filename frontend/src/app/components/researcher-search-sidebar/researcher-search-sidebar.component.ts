/**
 * Researcher Search — drops into the top bar as a search input + dropdown.
 */
import { Component, inject, signal, computed, HostListener } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { debounceTime, distinctUntilChanged, switchMap, of, Subject } from 'rxjs';
import { toSignal } from '@angular/core/rxjs-interop';

import { LitrixApiService } from '../../services/litrix-api.service';
import { ResearcherStats } from '../../models/litrix.models';

@Component({
  selector: 'app-researcher-search-sidebar',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="relative w-72">
      <input
        type="text"
        [(ngModel)]="query"
        (ngModelChange)="onQueryChange($event)"
        (focus)="showResults.set(true)"
        placeholder="ابحث عن باحث..."
        class="w-full px-4 py-2 pr-10 text-sm
               bg-ink-50 border border-ink-200 rounded-full
               placeholder:text-ink-400
               focus:outline-none focus:ring-2 focus:ring-accent/20
               focus:border-accent transition-all"
      />
      <svg class="absolute right-3 top-2.5 w-4 h-4 text-ink-400" fill="none"
           stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
      </svg>

      @if (showResults() && query.length > 0) {
        <div class="absolute top-full mt-2 right-0 w-96
                    bg-white border border-ink-200 rounded-apple shadow-hover
                    max-h-96 overflow-y-auto z-30">
          @if (loading()) {
            <div class="text-xs text-ink-400 text-center py-6">
              جاري البحث...
            </div>
          } @else if (resultsList().length === 0) {
            <div class="text-xs text-ink-500 text-center py-6">
              ما لقينا أحد بهذا الاسم
            </div>
          } @else {
            <ul class="py-2">
              @for (r of resultsList(); track r.user_id) {
                <li>
                  <button
                    (click)="select(r)"
                    class="w-full text-right px-4 py-2.5
                           hover:bg-ink-50 transition-colors group">
                    <div class="text-sm font-medium text-ink-700
                                group-hover:text-ink-900 line-clamp-1">
                      {{ r.full_name_ar || r.full_name_en }}
                    </div>
                    <div class="flex items-center gap-2 mt-0.5">
                      <span class="text-[10px] text-ink-500">
                        {{ r.department_name || '—' }}
                      </span>
                      <span class="text-[10px] text-ink-300">·</span>
                      <span class="text-[10px] text-ink-500">
                        {{ r.total_papers }} ابحاث
                      </span>
                    </div>
                  </button>
                </li>
              }
            </ul>
          }
        </div>
      }
    </div>
  `,
})
export class ResearcherSearchSidebarComponent {
  private readonly api = inject(LitrixApiService);
  private readonly router = inject(Router);

  query = '';
  private readonly query$ = new Subject<string>();
  readonly loading = signal<boolean>(false);
  readonly showResults = signal<boolean>(false);

  readonly results = toSignal(
    this.query$.pipe(
      debounceTime(300),
      distinctUntilChanged(),
      switchMap(q => {
        if (!q || q.length < 2) {
          this.loading.set(false);
          return of({ results: [] });
        }
        this.loading.set(true);
        return this.api.searchResearchers(q);
      }),
    ),
    { initialValue: { results: [] } }
  );

  readonly resultsList = computed(() => {
    const r = this.results();
    this.loading.set(false);
    return r.results || [];
  });

  onQueryChange(q: string) {
    this.query$.next(q);
    this.showResults.set(true);
    if (q.length >= 2) this.loading.set(true);
    else this.loading.set(false);
  }

  select(r: ResearcherStats) {
    this.query = '';
    this.showResults.set(false);
    this.router.navigate(['/researcher', r.user_id]);
  }

  @HostListener('document:click', ['$event'])
  onDocClick(ev: MouseEvent) {
    const target = ev.target as HTMLElement;
    if (!target.closest('app-researcher-search-sidebar')) {
      this.showResults.set(false);
    }
  }
}
