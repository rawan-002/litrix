// Departments page. Cards expand inline into a researcher list instead of
// navigating to a detail route — deans compare departments at a glance and
// inline expand keeps the context while jumping between cards.
import {
  Component, computed, inject, signal, effect, untracked,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';
import { AffiliationService } from '../../core/services/affiliation.service';


interface DeptCard {
  department_id: number;
  department_name: string;
  college_id: number | null;
  total_researchers: number;
  active_researchers: number;
  total_papers: number;
  total_citations: number;
  total_q1_papers: number;
  total_q2_papers: number;
  total_q3_papers: number;
  total_q4_papers: number;
  total_scopus_papers: number;
  total_isi_papers: number;
  journal_papers: number;
  conference_papers: number;
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
  private affiliation = inject(AffiliationService);

  readonly departments = signal<DeptCard[]>([]);
  readonly loading     = signal(true);
  readonly error       = signal<string | null>(null);

  readonly sortBy = signal<SortKey>('total_papers');
  readonly search = signal('');

  // Aliases the platform-wide switch (header toggle); the constructor effect
  // re-fetches the cards when it flips.
  readonly albahaOnly = this.affiliation.albahaOnly;

  readonly expandedId = signal<number | null>(null);
  readonly researchers = signal<ResearcherRow[]>([]);
  readonly resLoading = signal(false);

  // Distinct head-count from the overview endpoint. Summing total_researchers
  // across cards double-counts anyone in two departments, so prefer this.
  readonly headcount = signal<number | null>(null);

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

  // Header-strip totals across all departments.
  readonly totals = computed(() => {
    const list = this.departments();
    // The de-duped head-count only helps when several departments show; a HoD
    // sees one already-scoped card, so fall back to its own count there.
    const useOverviewCount = list.length > 1 && this.headcount() != null;
    return {
      depts:       list.length,
      researchers: useOverviewCount
                   ? this.headcount()!
                   : list.reduce((s, d) => s + (d.total_researchers || 0), 0),
      papers:      list.reduce((s, d) => s + (d.total_papers || 0), 0),
      citations:   list.reduce((s, d) => s + (d.total_citations || 0), 0),
    };
  });

  constructor() {
    // The header's Al-Baha toggle re-fetches the cards (and collapses any open
    // department, whose numbers change with the filter). Runs once on init too.
    effect(() => {
      this.affiliation.albahaOnly();
      untracked(() => { this.expandedId.set(null); this.refresh(); });
    });
    this.api.getOverview().subscribe({
      next: (r: any) => {
        const n = r?.totals?.researchers;
        if (typeof n === 'number') this.headcount.set(n);
      },
      error: () => { /* fall back to the card sum */ },
    });
  }

  refresh() {
    // Keep the cards up while re-fetching (e.g. after toggling the filter) so
    // the page never blanks out.
    if (this.departments().length === 0) this.loading.set(true);
    this.api.listDepartments({
      ordering: '-total_papers', albahaOnly: this.albahaOnly(),
    }).subscribe({
      next: (r: any) => {
        // Response is either {results: [...]} when paginated or a bare array.
        const items = r?.results ?? r ?? [];
        this.departments.set(items);
        this.loading.set(false);
      },
      error: e => {
        if (this.departments().length === 0) {
          this.error.set(e?.error?.error || 'Failed to load departments');
        }
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
    this.api.getDepartmentResearchers(d.department_id, this.albahaOnly()).subscribe({
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

  // Ranks arrive both in Arabic (from the scrape) and English camelCase (from
  // the registration dropdown). Normalise both to one clean English label;
  // keys are space-stripped + lower-cased so both forms resolve, and unknown
  // values pass through unchanged rather than showing a dash.
  private static readonly RANK_EN: Record<string, string> = {
    'أستاذ':         'Professor',
    'أستاذمشارك':    'Associate Professor',
    'أستاذمساعد':    'Assistant Professor',
    'محاضر':         'Lecturer',
    'معيد':          'Teaching Assistant',
    'professor':           'Professor',
    'associateprofessor':  'Associate Professor',
    'assistantprofessor':  'Assistant Professor',
    'lecturer':            'Lecturer',
    'teachingassistant':   'Teaching Assistant',
    'demonstrator':        'Teaching Assistant',
  };

  rankLabel(rank: string | null | undefined): string {
    if (!rank) return '—';
    const key = rank.trim().toLowerCase().replace(/\s+/g, '');
    return DepartmentsComponent.RANK_EN[key] ?? rank.trim();
  }
}
