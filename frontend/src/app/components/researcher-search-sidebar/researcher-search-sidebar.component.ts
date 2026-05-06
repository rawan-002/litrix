/**
 * Right Sidebar — All Researchers list with search filter.
 *
 * UX:
 *   - On load: fetch all researchers, display all
 *   - Search box at top: filters the visible list (client-side, instant)
 *   - Click a researcher → navigate to their profile
 */
import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';

interface ResearcherListItem {
  user_id: number;
  full_name_ar: string | null;
  full_name_en: string | null;
  department_name: string | null;
  papers: number;
}

@Component({
  selector: 'app-researcher-search-sidebar',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="h-full flex flex-col">
      <!-- Header -->
      <div class="px-5 pt-5 pb-3 border-b border-ink-200">
        <h2 class="text-xs uppercase tracking-widest text-ink-500 font-medium mb-1">
          Researchers
        </h2>
        <p class="text-xs text-ink-400">
          {{ filtered().length }} of {{ all().length }}
        </p>
      </div>

      <!-- Search filter -->
      <div class="px-5 py-3 border-b border-ink-200">
        <div class="relative">
          <input
            type="text"
            [(ngModel)]="filter"
            placeholder="Filter by name..."
            class="w-full px-3 py-2 pr-9 text-sm
                   bg-ink-50 border border-ink-200 rounded-apple
                   placeholder:text-ink-400
                   focus:outline-none focus:ring-2 focus:ring-accent/20
                   focus:border-accent transition-all"
          />
          <svg class="absolute right-2.5 top-2.5 w-4 h-4 text-ink-400" fill="none"
               stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
          </svg>
        </div>
      </div>

      <!-- Researchers list (scrollable) -->
      <div class="flex-1 overflow-y-auto px-3 py-3">
        @if (loading()) {
          <div class="text-xs text-ink-400 text-center py-6">Loading...</div>
        } @else if (filtered().length === 0) {
          <div class="text-xs text-ink-400 text-center py-6">
            No researchers found
          </div>
        } @else {
          <ul class="space-y-1">
            @for (r of filtered(); track r.user_id) {
              <li>
                <button
                  (click)="select(r)"
                  class="w-full text-right px-3 py-2 rounded-apple
                         hover:bg-ink-50 transition-colors group">
                  <div class="text-sm font-medium text-ink-700
                              group-hover:text-ink-900 line-clamp-1">
                    {{ r.full_name_ar || r.full_name_en }}
                  </div>
                  <div class="flex items-center gap-2 mt-0.5">
                    <span class="text-[10px] text-ink-500 line-clamp-1 flex-1">
                      {{ r.department_name || '—' }}
                    </span>
                    <span class="text-[10px] text-ink-400 shrink-0">
                      {{ r.papers }} pubs
                    </span>
                  </div>
                </button>
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class ResearcherSearchSidebarComponent implements OnInit {
  private readonly api = inject(LitrixApiService);
  private readonly router = inject(Router);

  readonly all = signal<ResearcherListItem[]>([]);
  readonly loading = signal<boolean>(true);
  filter = '';

  readonly filtered = computed(() => {
    const q = this.filter.trim().toLowerCase();
    const list = this.all();
    if (!q) return list;
    return list.filter(r =>
      (r.full_name_ar || '').toLowerCase().includes(q) ||
      (r.full_name_en || '').toLowerCase().includes(q) ||
      (r.department_name || '').toLowerCase().includes(q)
    );
  });

  ngOnInit() {
    this.api.getAllResearchers().subscribe({
      next: payload => {
        this.all.set(payload.results || []);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
      },
    });
  }

  select(r: ResearcherListItem) {
    this.router.navigate(['/researcher', r.user_id]);
  }
}
