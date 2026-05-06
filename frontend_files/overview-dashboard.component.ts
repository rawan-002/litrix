/**
 * Overview Dashboard — the landing page.
 *
 * Apple-style layout principles applied here:
 *   • Generous spacing (py-12, gap-6, gap-8)
 *   • Subtle card shadows (shadow-card)
 *   • Limited color palette (ink + accent only)
 *   • Typography hierarchy via size + weight, not color
 *   • Numbers are HUGE (text-5xl) — they're the protagonists
 *
 * Place in: src/app/components/overview-dashboard/overview-dashboard.component.ts
 */
import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { LitrixApiService } from '../../services/litrix-api.service';
import { OverviewPayload } from '../../models/litrix.models';

@Component({
  selector: 'app-overview-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './overview-dashboard.component.html',
})
export class OverviewDashboardComponent implements OnInit {
  private readonly api = inject(LitrixApiService);

  readonly data    = signal<OverviewPayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  ngOnInit() {
    this.api.getOverview().subscribe({
      next: (payload) => {
        this.data.set(payload);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`Failed to load: ${err.message}`);
        this.loading.set(false);
      },
    });
  }

  /** Pretty number formatter: 1234 → "1,234"; null → "—" */
  fmt(n: number | null | undefined): string {
    if (n == null) return '—';
    return n.toLocaleString('en-US');
  }
}
