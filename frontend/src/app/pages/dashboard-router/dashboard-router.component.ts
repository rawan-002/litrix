import { Component, inject } from '@angular/core';
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
@Component({
  selector: 'app-dashboard-router',
  standalone: true,
  imports: [
    CommonModule, OverviewDashboardComponent, ResearcherDashboardComponent,
  ],
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
