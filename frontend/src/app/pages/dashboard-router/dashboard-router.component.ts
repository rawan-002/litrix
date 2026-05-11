import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AuthService } from '../../core/services/auth.service';
import { OverviewDashboardComponent } from
  '../../components/overview-dashboard/overview-dashboard.component';
import { ResearcherDashboardComponent } from
  '../../components/researcher-dashboard/researcher-dashboard.component';


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
  imports: [CommonModule, OverviewDashboardComponent, ResearcherDashboardComponent],
  template: `
    @if (auth.hasPermission('view_all_researchers') ||
         auth.hasPermission('view_dept_researchers')) {
      <app-overview-dashboard></app-overview-dashboard>
    } @else if (auth.user()?.litrix_id; as lid) {
      <app-researcher-dashboard [litrixId]="lid"></app-researcher-dashboard>
    }
  `,
})
export class DashboardRouterComponent {
  protected auth = inject(AuthService);
}
