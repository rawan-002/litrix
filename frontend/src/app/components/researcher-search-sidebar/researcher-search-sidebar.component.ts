/**
 * Researcher Search Sidebar.
 *
 * Why debounced search (300ms): avoid hammering the API on every
 * keystroke. 300ms is the human "intentional pause" threshold.
 *
 * Uses the project's ink-* color palette (defined in tailwind.config.js)
 * for visual consistency with the rest of the dashboard.
 */
import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { debounceTime, distinctUntilChanged, switchMap, of, Subject } from 'rxjs';
import { toSignal } from '@angular/core/rxjs-interop';

import { LitrixApiService } from '../../services/litrix-api.service';
import { ResearcherStats } from '../../models/litrix.models';

@Component({
  selector: 'app-researcher-search-sidebar',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div class="p-5">
      <!-- Header -->
      <div class="mb-6">
        <h2 class="text-xs uppercase tracking-widest text-ink-400 font-medium">
          البحث
        </h2>
        <p class="text-lg font-semibold text-ink-700 mt-1">
          عن الباحثين
        </p>
      </div>

      <!-- Search input -->
      <div class="relative mb-4">
        <input
          type="text"
          [(ngModel)]="query"
          (ngModelChange)="onQueryChange($event)"
          placeholder="ابحث بالاسم..."
          class="w-full px-4 py-2.5 pr-10 text-sm
                 bg-ink-50 border border-ink-200 rounded-apple
                 placeholder:text-ink-400
                 focus:outline-none focus:ring-2 focus:ring-accent/20
                 focus:border-accent transition-all"
        />
        <svg class="absolute right-3 top-3 w-4 h-4 text-ink-400" fill="none"
             stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
        </svg>
      </div>

      <!-- Results -->
      @if (loading()) {
        <div class="text-xs text-ink-400 text-center py-6">
          جاري البحث...
        </div>
      } @else if (query.length === 0) {
        <div class="text-xs text-ink-400 text-center py-6 leading-relaxed">
          اكتبي اسم باحث لعرض ملفه الكامل،<br/>
          ابحاثه، وتحليل citations لكل سنة
        </div>
      } @else if (resultsList().length === 0) {
        <div class="text-xs text-ink-500 text-center py-6">
          ما لقينا أحد بهذا الاسم
        </div>
      } @else {
        <ul class="space-y-1.5">
          @for (r of resultsList(); track r.user_id) {
            <li>
              <button
                (click)="select(r)"
                class="w-full text-right px-3 py-2.5 rounded-apple
                       hover:bg-ink-50 hover:shadow-card
                       border border-transparent hover:border-ink-200
                       transition-all group">
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

      <!-- Footer link to overview -->
      <div class="mt-8 pt-5 border-t border-ink-200">
        <a routerLink="/" class="block text-xs text-ink-500 hover:text-ink-700
                                  transition-colors text-center">
          ← العودة إلى لوحة التحكم
        </a>
      </div>
    </div>
  `,
})
export class ResearcherSearchSidebarComponent {
  private readonly api = inject(LitrixApiService);
  private readonly router = inject(Router);

  query = '';
  private readonly query$ = new Subject<string>();
  readonly loading = signal<boolean>(false);

  readonly results = toSignal(
    this.query$.pipe(
      debounceTime(300),
      distinctUntilChanged(),
      switchMap(q => {
        if (!q || q.length < 2) {
          this.loading.set(false);
          return of({ count: 0, results: [], next: null, previous: null });
        }
        this.loading.set(true);
        return this.api.searchResearchers(q);
      }),
    ),
    { initialValue: { count: 0, results: [], next: null, previous: null } }
  );

  readonly resultsList = computed(() => this.results().results);

  onQueryChange(q: string) {
    this.query$.next(q);
    if (q.length >= 2) this.loading.set(true);
    else this.loading.set(false);
  }

  select(r: ResearcherStats) {
    this.router.navigate(['/researcher', r.user_id]);
    this.loading.set(false);
  }
}
