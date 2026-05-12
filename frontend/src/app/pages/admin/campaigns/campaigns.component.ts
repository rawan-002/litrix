/**
 * Admin Campaigns — Apple-style management page for reporting cycles.
 *
 * Layout philosophy:
 *   • Single-page list + inline detail panel (no separate detail route)
 *   • "+ New Campaign" opens a modal — the rest of the page stays put
 *   • Cards carry their own action buttons (Open / Close / View)
 *   • Status badges colour-coded: draft=ink, active=accent, closed=mute
 */
import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { LitrixApiService } from '../../../services/litrix-api.service';


interface Campaign {
  campaign_id: number;
  title: string;
  description: string | null;
  target_years: number[];
  opens_at: string;
  closes_at: string;
  status: 'draft' | 'active' | 'closed' | 'archived';
  scope_type: 'all' | 'department' | 'custom';
  scope_filter: Record<string, unknown> | null;
  created_by_user_id: number | null;
  created_at: string;
  closed_at: string | null;
  submissions_total: number;
  submissions_submitted: number;
  submissions_late: number;
}


interface SubmissionRow {
  SubmissionID: number;
  UserID: number;
  Status: string;
  FullName_Ar: string | null;
  Email: string;
  Litrix_ID: string | null;
  DepartmentName: string | null;
  IsLate: boolean;
  StartedAt: string | null;
  SubmittedAt: string | null;
  confirmed_count: number;
  not_mine_count: number;
  missing_count: number;
}


@Component({
  selector: 'app-admin-campaigns',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './campaigns.component.html',
})
export class CampaignsComponent {
  private api = inject(LitrixApiService);

  readonly campaigns = signal<Campaign[]>([]);
  readonly loading   = signal(true);
  readonly error     = signal<string | null>(null);
  readonly busyId    = signal<number | null>(null);

  // Expanded campaign — shows its submissions inline below the card.
  readonly expandedId  = signal<number | null>(null);
  readonly submissions = signal<SubmissionRow[]>([]);
  readonly subsLoading = signal(false);

  // Create modal state
  readonly showCreate = signal(false);
  readonly draft = signal({
    title:         '',
    description:   '',
    target_years:  this.defaultYears(),
    opens_at:      this.isoToday(),
    closes_at:     this.isoIn(7),
    scope_type:    'all' as 'all' | 'department' | 'custom',
  });

  // Filter pills at the top
  readonly statusFilter = signal<'all' | 'draft' | 'active' | 'closed'>('all');

  readonly filtered = computed(() => {
    const f = this.statusFilter();
    return f === 'all'
      ? this.campaigns()
      : this.campaigns().filter(c => c.status === f);
  });

  constructor() {
    this.refresh();
  }

  refresh() {
    this.loading.set(true);
    this.api.listCampaigns().subscribe({
      next: r => {
        this.campaigns.set(r.campaigns || []);
        this.loading.set(false);
      },
      error: e => {
        this.error.set(this.errMsg(e));
        this.loading.set(false);
      },
    });
  }

  openCreate() {
    this.draft.set({
      title:         '',
      description:   '',
      target_years:  this.defaultYears(),
      opens_at:      this.isoToday(),
      closes_at:     this.isoIn(7),
      scope_type:    'all',
    });
    this.showCreate.set(true);
  }

  submitCreate() {
    const d = this.draft();
    if (!d.title.trim()) {
      this.error.set('Title is required');
      return;
    }
    if (d.target_years.length === 0) {
      this.error.set('Pick at least one target year');
      return;
    }

    this.api.createCampaign({
      title:        d.title.trim(),
      description:  d.description.trim() || undefined,
      target_years: d.target_years,
      opens_at:     new Date(d.opens_at).toISOString(),
      closes_at:    new Date(d.closes_at).toISOString(),
      scope_type:   d.scope_type,
      scope_filter: {},
    }).subscribe({
      next: () => {
        this.showCreate.set(false);
        this.error.set(null);
        this.refresh();
      },
      error: e => this.error.set(this.errMsg(e)),
    });
  }

  open(c: Campaign) {
    this.busyId.set(c.campaign_id);
    this.api.openCampaign(c.campaign_id).subscribe({
      next: r => {
        this.busyId.set(null);
        this.refresh();
      },
      error: e => {
        this.busyId.set(null);
        this.error.set(this.errMsg(e));
      },
    });
  }

  close(c: Campaign) {
    if (!confirm(`Close "${c.title}"? Researchers will no longer be able to submit.`)) {
      return;
    }
    this.busyId.set(c.campaign_id);
    this.api.closeCampaign(c.campaign_id).subscribe({
      next: () => {
        this.busyId.set(null);
        this.refresh();
      },
      error: e => {
        this.busyId.set(null);
        this.error.set(this.errMsg(e));
      },
    });
  }

  toggleExpand(c: Campaign) {
    if (this.expandedId() === c.campaign_id) {
      this.expandedId.set(null);
      return;
    }
    this.expandedId.set(c.campaign_id);
    this.subsLoading.set(true);
    this.api.listCampaignSubmissions(c.campaign_id).subscribe({
      next: r => {
        this.submissions.set(r.submissions || []);
        this.subsLoading.set(false);
      },
      error: () => this.subsLoading.set(false),
    });
  }

  /**
   * Trigger the xlsx download. We read the response as a Blob, build a
   * temporary object URL, and click a synthetic <a download> — the
   * standard browser pattern for save-as-file from XHR.
   */
  exportToExcel(c: Campaign) {
    this.busyId.set(c.campaign_id);
    this.api.exportCampaign(c.campaign_id).subscribe({
      next: blob => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        // Filename is set server-side via Content-Disposition; this is
        // just a fallback if the browser ignores the header.
        a.download = `campaign_${c.campaign_id}.xlsx`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        this.busyId.set(null);
      },
      error: e => {
        this.busyId.set(null);
        this.error.set(this.errMsg(e));
      },
    });
  }

  toggleYear(year: number) {
    const cur = this.draft().target_years;
    if (cur.includes(year)) {
      this.draft.update(d => ({
        ...d, target_years: d.target_years.filter(y => y !== year),
      }));
    } else {
      this.draft.update(d => ({
        ...d, target_years: [...d.target_years, year].sort(),
      }));
    }
  }

  isYearSelected(year: number): boolean {
    return this.draft().target_years.includes(year);
  }

  // -- helpers ----------------------------------------------------------
  yearOptions(): number[] {
    const cur = new Date().getFullYear();
    return [cur - 1, cur, cur + 1];
  }

  defaultYears(): number[] {
    const cur = new Date().getFullYear();
    return [cur - 1, cur];
  }

  isoToday(): string {
    return new Date().toISOString().slice(0, 16);
  }

  isoIn(days: number): string {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return d.toISOString().slice(0, 16);
  }

  /**
   * Pull the most useful message out of any HttpErrorResponse shape.
   * Backend returns { error, message? } on validation failures.
   */
  private errMsg(e: any): string {
    return e?.error?.error || e?.error?.detail || e?.message
        || 'Something went wrong';
  }

  progressPct(c: Campaign): number {
    if (!c.submissions_total) return 0;
    return Math.round(100 * c.submissions_submitted / c.submissions_total);
  }

  daysUntilClose(c: Campaign): number {
    const ms = new Date(c.closes_at).getTime() - Date.now();
    return Math.max(0, Math.ceil(ms / (1000 * 60 * 60 * 24)));
  }
}
