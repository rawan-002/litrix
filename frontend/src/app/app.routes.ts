/**
 * App Routes — single-page navigation map.
 * Place in: src/app/app.routes.ts
 */
import { Routes } from '@angular/router';
import { OverviewDashboardComponent } from
  './components/overview-dashboard/overview-dashboard.component';

export const routes: Routes = [
  { path: '', component: OverviewDashboardComponent },
  { path: '**', redirectTo: '' },
];
