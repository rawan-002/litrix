/**
 * Departments page — institutional drill-down.
 *
 * Layout:
 *   [Sort pills]                           ← order by papers / citations / h-index
 *   [Department cards grid]                ← each shows KPIs
 *      └ [click] → inline researcher list  ← name, papers, citations, h-index
 *
 * Why one-page-with-inline-expand instead of /departments/:id detail?
 *   Academic data is read-heavy and exploratory — admins/deans want
 *   to compare departments at a glance. Inline expansion keeps context
 *   so closing one card and opening another is friction-free.
 */
import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';


interface DeptCard {
  department_id: number;
  department_name: string;
  college_id: number | null;
  total_researchers: number;
  active_researchers: number;
  total_papers: number;
  total_citations: number;
  total_q1_papers: number;
  total_scopus_papers: number;
  total_isi_papers: number;
  avg_h_index: number;
  max_h_index: number;
}

interface ResearcherRow {
  user_id: number;
  litrix_id: string | null;
  full_name_ar: string | null;
  full_name_en: string | null;
  academic_rank: string | null;
  total_papers: number;
  total_citations: number;
  h_index: number;
  q1_papers: number;
}

type SortKey = 'total_papers' | 'total_citations' | 'avg_h_index' | 'total_researchers';


@Component({
  selector: 'app-departments',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './departments.component.html',
})
export class DepartmentsComponent {
  private api = inject(LitrixApiService);

  readonly departments = signal<DeptCard[]>([]);
  readonly loading     = signal(true);
  readonly error       = signal<string | null>(null);

  readonly sortBy = signal<SortKey>('total_papers');
  readonly search = signal('');

  readonly expandedId = signal<number | null>(null);
  readonly researchers = signal<ResearcherRow[]>([]);
  readonly resLoading = signal(false);

  readonly sorted = computed(() => {
    const key = this.sortBy();
    const q   = this.search().toLowerCase().trim();
    let list = this.departments();
    if (q) {
      list = list.filter(d =>
        (d.department_name || '').toLowerCase().includes(q)
      );
    }
    return [...list].sort((a, b) => (b[key] ?? 0) - (a[key] ?? 0));
  });

  /** Sum across all departments, for the header strip. */
  readonly totals = computed(() => {
    const list = this.departments();
    return {
      depts:       list.length,
      researchers: list.reduce((s, d) => s + (d.total_researchers || 0), 0),
      papers:      list.reduce((s, d) => s + (d.total_papers || 0), 0),
      citations:   list.reduce((s, d) => s + (d.total_citations || 0), 0),
    };
  });

  constructor() {
    this.refresh();
  }

  refresh() {
    this.loading.set(true);
    this.api.listDepartments({ ordering: '-total_papers' }).subscribe({
      next: (r: any) => {
        // Response can be either {results: [...]} (paginated) or [...]
        const items = r?.results ?? r ?? [];
        this.departments.set(items);
        this.loading.set(false);
      },
      error: e => {
        this.error.set(e?.error?.error || 'Failed to load departments');
        this.loading.set(false);
      },
    });
  }

  toggleExpand(d: DeptCard) {
    if (this.expandedId() === d.department_id) {
      this.expandedId.set(null);
      return;
    }
    this.expandedId.set(d.department_id);
    this.resLoading.set(true);
    this.api.getDepartmentResearchers(d.department_id).subscribe({
      next: (r: any) => {
        const items = r?.results ?? r ?? [];
        this.researchers.set(items);
        this.resLoading.set(false);
      },
      error: () => {
        this.researchers.set([]);
        this.resLoading.set(false);
      },
    });
  }

  setSort(key: SortKey) {
    this.sortBy.set(key);
  }

  sortLabel(key: SortKey): string {
    return {
      total_papers:      'Papers',
      total_citations:   'Citations',
      avg_h_index:       'Avg h-index',
      total_researchers: 'Researchers',
    }[key];
  }
}
