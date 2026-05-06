/**
 * App Routes — single-page navigation map.
 */
import { Routes } from '@angular/router';
import { OverviewDashboardComponent } from
  './components/overview-dashboard/overview-dashboard.component';
import { ResearcherProfileComponent } from
  './components/researcher-profile/researcher-profile.component';

export const routes: Routes = [
  { path: '',                 component: OverviewDashboardComponent },
  { path: 'researcher/:id',   component: ResearcherProfileComponent },
  { path: '**',               redirectTo: '' },
];
