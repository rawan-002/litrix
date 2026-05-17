/**
 * My Reports — researcher's landing page for verification campaigns.
 *
 * Shows two stacks:
 *   1. Active submissions    — those needing action (badge in sidebar)
 *   2. Past submissions      — submitted / closed (read-only history)
 *
 * Each submission row links to /my-reports/<submission_id> where the
 * researcher actually verifies the paper list.
 */
import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';


interface SubmissionEntry {
  SubmissionID: number;
  Status: 'pending' | 'in_progress' | 'submitted' | 'reopened';
  StartedAt: string | null;
  SubmittedAt: string | null;
  IsLate: boolean;
  CampaignID: number;
  Title: string;
  Description: string | null;
  TargetYears: number[];
  ClosesAt: string;
  campaign_status: 'active' | 'closed' | 'archived';
  decisions_count: number;
}


@Component({
  selector: 'app-my-reports',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './my-reports.component.html',
})
export class MyReportsComponent {
  private api = inject(LitrixApiService);

  readonly submissions = signal<SubmissionEntry[]>([]);
  readonly loading     = signal(true);

  readonly active = computed(() =>
    this.submissions().filter(s =>
      s.campaign_status === 'active' && s.Status !== 'submitted'
    )
  );

  readonly past = computed(() =>
    this.submissions().filter(s =>
      s.Status === 'submitted' || s.campaign_status !== 'active'
    )
  );

  constructor() {
    this.api.getMyReports().subscribe({
      next: r => {
        this.submissions.set(r.submissions || []);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  daysLeft(closesAt: string): number {
    const ms = new Date(closesAt).getTime() - Date.now();
    return Math.max(0, Math.ceil(ms / (1000 * 60 * 60 * 24)));
  }

  statusLabel(s: SubmissionEntry): string {
    if (s.Status === 'submitted') return 'Submitted';
    if (s.Status === 'in_progress') return 'In Progress';
    if (s.Status === 'reopened')    return 'Reopened';
    return 'Not Started';
  }
}
