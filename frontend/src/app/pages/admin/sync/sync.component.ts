import { Component, inject, signal, computed, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface Researcher {
  UserID: number;
  Litrix_ID: string;
  FullName_Ar: string | null;
  Email: string;
  Scholar_ID: string | null;
  Orcid_ID: string | null;
  LastSyncedAt: string | null;
  papers: number;
  // Backend flag: synced within SYNC_COOLDOWN_DAYS, so we disable the regular
  // Sync button and only offer Force.
  is_in_cooldown: boolean;
  cooldown_until: string | null;
}

interface SyncJob {
  JobID: number;
  Source: string;
  Status: string;
  StartedAt: string | null;
  FinishedAt: string | null;
  ErrorMessage: string | null;
  CreatedAt: string;
  UserID: number;
  FullName_Ar: string | null;
  FirstName: string | null;
  LastName: string | null;
  Email: string | null;
  triggered_by_name: string | null;
}


@Component({
  selector: 'app-sync',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './sync.component.html',
})
export class SyncComponent implements OnDestroy {
  private http = inject(HttpClient);
  private API = `${environment.apiBaseUrl}/auth/sync`;

  readonly researchers = signal<Researcher[]>([]);
  readonly jobs = signal<SyncJob[]>([]);
  readonly loading = signal(true);
  readonly search = signal('');
  readonly triggering = signal<number | null>(null);
  readonly cooldownDays = signal<number>(7);

  // Citation refresh — numbers only, never adds or edits papers.
  readonly citScope = signal<'missing' | 'all'>('missing');
  readonly citBudget = signal(0);
  readonly citTriggering = signal(false);
  // True while a citations job is queued/running, which disables the button.
  readonly citRunning = computed(() =>
    this.jobs().some(j =>
      j.Source === 'citations' &&
      (j.Status === 'queued' || j.Status === 'running')),
  );

  // Incremental scrape — new papers only, never re-downloads existing ones.
  readonly newBudget = signal(100);
  readonly newTriggering = signal(false);
  readonly newRunning = computed(() =>
    this.jobs().some(j =>
      j.Source === 'scrape_new' &&
      (j.Status === 'queued' || j.Status === 'running')),
  );

  readonly filtered = computed(() => {
    const q = this.search().toLowerCase();
    return q
      ? this.researchers().filter(r =>
          r.Email.toLowerCase().includes(q) ||
          (r.FullName_Ar || '').toLowerCase().includes(q))
      : this.researchers();
  });

  private pollHandle: any;

  constructor() {
    this.load();
    this.loadJobs();
    this.pollHandle = setInterval(() => this.loadJobs(), 5000);
  }

  ngOnDestroy() {
    if (this.pollHandle) clearInterval(this.pollHandle);
  }

  load() {
    this.loading.set(true);
    this.http.get<{
      researchers: Researcher[];
      cooldown_days: number;
    }>(`${this.API}/researchers/`).subscribe({
      next: r => {
        this.researchers.set(r.researchers || []);
        this.cooldownDays.set(r.cooldown_days ?? 7);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  loadJobs() {
    this.http.get<{ jobs: SyncJob[] }>(`${this.API}/jobs/`).subscribe({
      next: r => this.jobs.set(r.jobs || []),
    });
  }

  // Trigger a sync. The backend enforces a cooldown: without force it may
  // return 409, which we catch and re-offer via confirm(); force=true is the
  // admin override used by the table's pre-confirmed Force buttons.
  trigger(userId: number, source: 'scholar' | 'orcid', force = false) {
    this.triggering.set(userId);
    this.http.post(`${this.API}/trigger/`, {
      user_id: userId, source, force,
    }).subscribe({
      next: () => {
        this.triggering.set(null);
        this.loadJobs();
        // Reload the row so LastSyncedAt reflects this run.
        setTimeout(() => this.load(), 1500);
      },
      error: err => {
        this.triggering.set(null);
        if (err.status === 409 && err.error?.error === 'in_cooldown' && !force) {
          // The table already disables Sync during cooldown, but a stale row
          // could still land here.
          const ok = confirm(
            `${err.error.message}\n\nProceed with a forced re-sync?`,
          );
          if (ok) this.trigger(userId, source, true);
          return;
        }
        alert(err.error?.error || err.error?.message || 'Failed to trigger sync');
      },
    });
  }

  forceSync(userId: number, source: 'scholar' | 'orcid', papers: number) {
    const ok = confirm(
      `This researcher already has ${papers} papers stored. ` +
      `A force re-sync will hit the SerpAPI quota again. Continue?`,
    );
    if (ok) this.trigger(userId, source, true);
  }

  // Institution-wide citation refresh. Numbers only — the backend updates
  // CitationsByYear and the displayed totals on existing papers; it never
  // inserts papers, edits metadata, or touches researcher links.
  triggerCitations() {
    const budget = this.citBudget() || 0;
    if (budget > 0) {
      const ok = confirm(
        `SerpAPI budget is ${budget}: papers OpenAlex can't resolve will ` +
        `use up to ${budget} PAID SerpAPI credits. Continue?`,
      );
      if (!ok) return;
    }
    this.citTriggering.set(true);
    this.http.post(`${this.API}/refresh-citations/`, {
      scope: this.citScope(),
      serp_budget: budget,
    }).subscribe({
      next: () => {
        this.citTriggering.set(false);
        this.loadJobs();
      },
      error: err => {
        this.citTriggering.set(false);
        alert(err.error?.message || err.error?.error || 'Failed to start citation refresh');
        this.loadJobs();
      },
    });
  }

  // Incremental new-papers scrape across all researchers. Per researcher it
  // fetches Scholar newest-first and stops at the first paper older than their
  // latest stored PubYear, so existing papers aren't touched. Roughly 1 SerpAPI
  // credit each, more only when they actually have new papers.
  triggerNewPapers() {
    const budget = this.newBudget() || 100;
    const ok = confirm(
      `Scrape NEW papers only across all researchers.\n` +
      `Each researcher costs at least 1 SerpAPI credit ` +
      `(budget cap: ${budget}). Continue?`,
    );
    if (!ok) return;
    this.newTriggering.set(true);
    this.http.post(`${this.API}/scrape-new/`, {
      serp_budget: budget,
    }).subscribe({
      next: () => {
        this.newTriggering.set(false);
        this.loadJobs();
      },
      error: err => {
        this.newTriggering.set(false);
        alert(err.error?.message || err.error?.error || 'Failed to start new-papers scrape');
        this.loadJobs();
      },
    });
  }

  daysUntil(iso: string | null): number | null {
    if (!iso) return null;
    const ms = new Date(iso).getTime() - Date.now();
    if (ms <= 0) return 0;
    return Math.ceil(ms / (1000 * 60 * 60 * 24));
  }

  // Best human-readable label for a job's researcher, falling back through the
  // name chain so "User #N" only shows when we know nothing else.
  jobLabel(j: SyncJob): string {
    if (j.FullName_Ar) return j.FullName_Ar;
    const en = [j.FirstName, j.LastName].filter(Boolean).join(' ').trim();
    if (en) return en;
    if (j.Email) return j.Email;
    // Institution-wide jobs carry no researcher.
    if (j.Source === 'citations') return 'All papers';
    if (j.Source === 'scrape_new') return 'All researchers';
    return `User #${j.UserID}`;
  }

  // Arabic names render RTL, everything else LTR — avoids the mixed-bidi mess.
  jobLabelDir(j: SyncJob): 'rtl' | 'ltr' {
    return j.FullName_Ar ? 'rtl' : 'ltr';
  }

  formatDate(s: string | null): string {
    if (!s) return 'Never';
    return new Date(s).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  }

  formatDateTime(s: string | null): string {
    if (!s) return '—';
    return new Date(s).toLocaleString('en-US', {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  statusBadge(s: string): string {
    const map: any = {
      queued:    'bg-ink-100 text-ink-600',
      running:   'bg-blue-50 text-blue-700',
      completed: 'bg-green-50 text-green-700',
      failed:    'bg-red-50 text-red-700',
    };
    return map[s] || map.queued;
  }
}
