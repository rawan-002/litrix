/**
 * Right Sidebar - Researchers grouped by department, with search filter.
 */
import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';

interface ResearcherListItem {
  user_id: number;
  litrix_id: string;
  full_name_ar: string | null;
  full_name_en: string | null;
  department_name: string | null;
  papers: number;
}

interface DeptGroup {
  department: string;
  researchers: ResearcherListItem[];
  expanded: boolean;
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
          {{ totalFiltered() }} of {{ all().length }}
        </p>
      </div>

      <!-- Search filter -->
      <div class="px-5 py-3 border-b border-ink-200">
        <div class="relative">
          <input
            type="text"
            [(ngModel)]="filter"
            placeholder="Search by name or department..."
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

      <!-- Department groups (scrollable) -->
      <div class="flex-1 overflow-y-auto px-3 py-3">
        @if (loading()) {
          <div class="text-xs text-ink-400 text-center py-6">Loading...</div>
        } @else if (totalFiltered() === 0) {
          <div class="text-xs text-ink-400 text-center py-6">
            No researchers found
          </div>
        } @else {
          <div class="space-y-3">
            @for (group of grouped(); track group.department) {
              <div>
                <!-- Department header (collapsible) -->
                <button
                  (click)="toggleGroup(group.department)"
                  class="w-full flex items-center justify-between
                         px-2 py-1.5 text-[11px] font-semibold uppercase
                         tracking-wider text-ink-600 hover:text-ink-900
                         transition-colors">
                  <span class="flex items-center gap-1.5">
                    <svg [class.rotate-90]="isExpanded(group.department)"
                         class="w-3 h-3 transition-transform"
                         fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round"
                            stroke-width="2.5" d="M9 5l7 7-7 7" />
                    </svg>
                    <span>{{ group.department }}</span>
                  </span>
                  <span class="text-ink-400 font-normal normal-case">
                    {{ group.researchers.length }}
                  </span>
                </button>

                <!-- Researchers in this department -->
                @if (isExpanded(group.department)) {
                  <ul class="list-none space-y-0.5 mt-1 ml-3 p-0">
                    @for (r of group.researchers; track r.user_id) {
                      <li class="list-none">
                        <button
                          (click)="select(r)"
                          class="w-full text-left px-3 py-2 rounded-apple
                                 hover:bg-ink-50 transition-colors group">
                          <div class="text-sm font-medium text-ink-700
                                      group-hover:text-ink-900 truncate">
                            {{ r.full_name_ar || r.full_name_en || ('User #' + r.user_id) }}
                          </div>
                          <div class="text-[10px] text-ink-400 mt-0.5">
                            {{ r.papers }} pubs
                          </div>
                        </button>
                      </li>
                    }
                  </ul>
                }
              </div>
            }
          </div>
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
  readonly expandedDepts = signal<Set<string>>(new Set());
  filter = '';

  // Group filtered researchers by department
  readonly grouped = computed<DeptGroup[]>(() => {
    const q = this.filter.trim().toLowerCase();
    const list = this.all();
    const filtered = !q ? list : list.filter(r =>
      (r.full_name_ar || '').toLowerCase().includes(q) ||
      (r.full_name_en || '').toLowerCase().includes(q) ||
      (r.department_name || '').toLowerCase().includes(q)
    );

    const groups = new Map<string, ResearcherListItem[]>();
    for (const r of filtered) {
      const dept = r.department_name || 'Unknown';
      if (!groups.has(dept)) groups.set(dept, []);
      groups.get(dept)!.push(r);
    }

    // Groups sorted by member count, researchers within a group by papers.
    return Array.from(groups.entries())
      .map(([department, researchers]) => ({
        department,
        researchers: researchers.sort((a, b) => (b.papers || 0) - (a.papers || 0)),
        expanded: false,
      }))
      .sort((a, b) => b.researchers.length - a.researchers.length);
  });

  readonly totalFiltered = computed(() =>
    this.grouped().reduce((sum, g) => sum + g.researchers.length, 0)
  );

  ngOnInit() {
    this.api.getAllResearchers().subscribe({
      next: payload => {
        const list = payload.results || [];
        this.all.set(list);
        // Expand every department by default.
        const depts = new Set<string>();
        list.forEach(r => depts.add(r.department_name || 'Unknown'));
        this.expandedDepts.set(depts);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  isExpanded(dept: string): boolean {
    return this.expandedDepts().has(dept);
  }

  toggleGroup(dept: string) {
    this.expandedDepts.update(set => {
      const next = new Set(set);
      if (next.has(dept)) next.delete(dept);
      else next.add(dept);
      return next;
    });
  }

  select(r: ResearcherListItem) {
    // Prefer the public Litrix-ID; fall back to user_id for un-migrated rows.
    const id = r.litrix_id || r.user_id;
    this.router.navigate(['/profile', id]);
  }
}
