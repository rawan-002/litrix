import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AuthService } from '../../core/services/auth.service';
import { OverviewDashboardComponent } from
  '../../components/overview-dashboard/overview-dashboard.component';
import { ResearcherDashboardComponent } from
  '../../components/researcher-dashboard/researcher-dashboard.component';


// Picks the home dashboard by permission: the institution overview for
// Admin/Dean/HoD, the analytics-only view for researchers. The full
// publications archive stays separate under /profile/Lit-NNNNNN so the home
// page is for insights and the profile is the authoritative archive.
//
// A user who is both (an admin role AND a working researcher) gets a third
// case: view_all_researchers/view_dept_researchers picks the Overview as the
// default landing tab, but view_personal_dashboard adds a second tab back to
// their own dashboard instead of losing access to it entirely.
@Component({
  selector: 'app-dashboard-router',
  standalone: true,
  imports: [
    CommonModule, OverviewDashboardComponent, ResearcherDashboardComponent,
  ],
  template: `
    @if (hasOverview()) {
      @if (hasPersonal(); as lid) {
        <div class="flex items-center gap-1 bg-ink-100 rounded-full p-1 mb-6 inline-flex">
          <button (click)="tab.set('overview')"
                  class="text-sm font-medium px-5 py-2 rounded-full transition"
                  [class.bg-white]="tab() === 'overview'"
                  [class.shadow-card]="tab() === 'overview'"
                  [class.text-ink-700]="tab() === 'overview'"
                  [class.text-ink-500]="tab() !== 'overview'">
            Overview
          </button>
          <button (click)="tab.set('personal')"
                  class="text-sm font-medium px-5 py-2 rounded-full transition"
                  [class.bg-white]="tab() === 'personal'"
                  [class.shadow-card]="tab() === 'personal'"
                  [class.text-ink-700]="tab() === 'personal'"
                  [class.text-ink-500]="tab() !== 'personal'">
            My Dashboard
          </button>
        </div>
        @if (tab() === 'overview') {
          <app-overview-dashboard></app-overview-dashboard>
        } @else {
          <app-researcher-dashboard [litrixId]="lid"></app-researcher-dashboard>
        }
      } @else {
        <app-overview-dashboard></app-overview-dashboard>
      }
    } @else if (auth.user()?.litrix_id; as lid) {
      <app-researcher-dashboard [litrixId]="lid"></app-researcher-dashboard>
    }
  `,
})
export class DashboardRouterComponent {
  protected auth = inject(AuthService);

  readonly tab = signal<'overview' | 'personal'>('overview');

  protected readonly hasOverview = computed(() =>
    this.auth.hasPermission('view_all_researchers') ||
    this.auth.hasPermission('view_dept_researchers')
  );

  /** Returns the user's own litrix_id when they're also entitled to the
   *  personal-dashboard tab, null otherwise - doubles as the @if guard and
   *  the [litrixId] input. */
  protected readonly hasPersonal = computed(() =>
    this.auth.hasPermission('view_personal_dashboard')
      ? (this.auth.user()?.litrix_id ?? null)
      : null
  );
}
