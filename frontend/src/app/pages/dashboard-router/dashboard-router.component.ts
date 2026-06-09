import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AuthService } from '../../core/services/auth.service';
import { OverviewDashboardComponent } from
  '../../components/overview-dashboard/overview-dashboard.component';
import { ResearcherDashboardComponent } from
  '../../components/researcher-dashboard/researcher-dashboard.component';
import { FireworksComponent } from '../../shared/fireworks/fireworks.component';


/**
 * Routes the home page (`/`) based on permissions:
 *   • Admin / Dean / HoD → OverviewDashboardComponent (institution view)
 *   • Researcher         → ResearcherDashboardComponent (analytics-only)
 *
 * The full publications archive lives under /profile/Lit-NNNNNN — kept
 * deliberately separate from the dashboard so the home page stays an
 * insights surface and the profile stays the authoritative archive.
 */
@Component({
  selector: 'app-dashboard-router',
  standalone: true,
  imports: [
    CommonModule, OverviewDashboardComponent, ResearcherDashboardComponent,
    FireworksComponent,
  ],
  template: `
    @if (auth.hasPermission('view_all_researchers') ||
         auth.hasPermission('view_dept_researchers')) {
      <app-overview-dashboard></app-overview-dashboard>
    } @else if (auth.user()?.litrix_id; as lid) {
      <app-researcher-dashboard [litrixId]="lid"></app-researcher-dashboard>
    }

    <!-- Warm one-per-session welcome for the project supervisor 💚 -->
    @if (showWelcome()) {
      <app-fireworks [name]="supervisorName()" (closed)="showWelcome.set(false)" />
    }
  `,
})
export class DashboardRouterComponent {
  protected auth = inject(AuthService);

  // The supervisor's account (UserID 1). Greet him with fireworks once per
  // session — a small thank-you for overseeing the project.
  private static readonly SUPERVISOR_USER_ID = 1;
  private static readonly SEEN_KEY = 'litrix_welcome_v1';

  readonly showWelcome = signal<boolean>(this.shouldWelcome());

  private shouldWelcome(): boolean {
    const u = this.auth.user();
    if (!u || u.user_id !== DashboardRouterComponent.SUPERVISOR_USER_ID) return false;
    try {
      if (sessionStorage.getItem(DashboardRouterComponent.SEEN_KEY)) return false;
      sessionStorage.setItem(DashboardRouterComponent.SEEN_KEY, '1');
    } catch { /* sessionStorage unavailable — just show it */ }
    return true;
  }

  supervisorName(): string {
    // Prefer a short, warm Arabic form: "د. " + first name if available.
    const ar = this.auth.user()?.full_name_ar || '';
    const first = ar.split(' ')[0] || 'دكتور';
    return `د. ${first}`;
  }
}
