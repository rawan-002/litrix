import { Routes } from '@angular/router';
import { inject } from '@angular/core';
import { AuthService } from './core/services/auth.service';
import { authGuard, guestGuard, permissionGuard } from './core/guards/auth.guard';

import { LayoutComponent } from './shared/layout/layout.component';
import { OverviewDashboardComponent } from
  './components/overview-dashboard/overview-dashboard.component';
import { ResearcherProfileComponent } from
  './components/researcher-profile/researcher-profile.component';
import { DashboardRouterComponent } from
  './pages/dashboard-router/dashboard-router.component';
import { LoginComponent } from './auth/login/login.component';
import { RegisterComponent } from './auth/register/register.component';
import { RegistrationsComponent } from
  './pages/admin/registrations/registrations.component';
import { UsersComponent } from './pages/admin/users/users.component';
import { AuditComponent } from './pages/admin/audit/audit.component';
import { SyncComponent } from './pages/admin/sync/sync.component';
import { RolesComponent } from './pages/admin/roles/roles.component';
import { ForgotPasswordComponent } from './auth/forgot-password/forgot-password.component';
import { SettingsComponent } from './pages/settings/settings.component';
import { SearchComponent } from './pages/search/search.component';
import { NotificationsComponent } from './pages/notifications/notifications.component';
import { InvitationsComponent } from './pages/admin/invitations/invitations.component';
import { CampaignsComponent } from './pages/admin/campaigns/campaigns.component';
import { MyReportsComponent } from './pages/my-reports/my-reports.component';
import { MyReportDetailComponent } from './pages/my-reports/my-report-detail.component';
import { DepartmentsComponent } from './pages/departments/departments.component';
import { WelcomeComponent } from './pages/welcome/welcome.component';
import { NetworkComponent } from './pages/network/network.component';

export const routes: Routes = [
  // Landing page for signed-out visitors. guestGuard bounces
  // already-authenticated users straight to /.
  { path: 'welcome',          component: WelcomeComponent,        canActivate: [guestGuard] },

  { path: 'login',            component: LoginComponent,          canActivate: [guestGuard] },
  { path: 'register',         component: RegisterComponent,       canActivate: [guestGuard] },
  { path: 'forgot-password',  component: ForgotPasswordComponent, canActivate: [guestGuard] },

  // Public dashboard — no login. Lazy standalone components deliberately
  // outside LayoutComponent so they skip the authenticated shell/sidebar.
  // Backed by the AllowAny /api/public/* endpoints.
  {
    path: 'public/dashboard',
    loadComponent: () =>
      import('./public/dashboard/public-dashboard.component')
        .then((m) => m.PublicDashboardComponent),
  },
  {
    path: 'public/researcher/:litrixId',
    loadComponent: () =>
      import('./public/profile/public-profile.component')
        .then((m) => m.PublicProfileComponent),
  },

  {
    path: '',
    component: LayoutComponent,
    canActivate: [authGuard],
    children: [
      { path: '',                component: DashboardRouterComponent },
      // Canonical profile URL, keyed on the public Lit-NNNNNN id.
      { path: 'profile/:litrixId', component: ResearcherProfileComponent },
      // Legacy alias so old links don't 404 — the component reads either param.
      { path: 'researcher/:id',    component: ResearcherProfileComponent },
      {
        path: 'admin/registrations',
        component: RegistrationsComponent,
        canActivate: [permissionGuard('approve_registrations')],
      },
      {
        path: 'admin/invitations',
        component: InvitationsComponent,
        canActivate: [permissionGuard('manage_users')],
      },
      {
        path: 'admin/users',
        component: UsersComponent,
        canActivate: [permissionGuard('manage_users')],
      },
      {
        path: 'admin/audit',
        component: AuditComponent,
        canActivate: [permissionGuard('view_audit_log')],
      },
      {
        path: 'admin/sync',
        component: SyncComponent,
        canActivate: [permissionGuard('trigger_sync')],
      },
      {
        path: 'admin/roles',
        component: RolesComponent,
        canActivate: [permissionGuard('manage_roles')],
      },
      {
        // Visible to anyone who can manage campaigns or view reports.
        path: 'admin/campaigns',
        component: CampaignsComponent,
        canActivate: [permissionGuard('manage_campaigns', 'view_campaign_reports')],
      },
      // Open to every authenticated user; the page shows an empty state
      // when there are no active campaigns, so authGuard alone is enough.
      { path: 'my-reports',         component: MyReportsComponent },
      { path: 'my-reports/:id',     component: MyReportDetailComponent },
      {
        path: 'departments',
        component: DepartmentsComponent,
        canActivate: [permissionGuard(
          'view_all_researchers', 'view_dept_researchers', 'manage_departments',
        )],
      },
      // Collaboration graph, open to all authenticated users — centred
      // on whoever's viewing by default.
      { path: 'network', component: NetworkComponent },

      // Chatbot surface. Lazy-loaded to keep the bundle lean; the RAG
      // backend lands later.
      {
        path: 'ai',
        loadComponent: () =>
          import('./pages/ai/litrix-ai.component')
            .then(m => m.LitrixAiComponent),
      },
      {
        // Redirect to the signed-in user's own /profile/Lit-NNNNNN. A
        // function (not a string) so we can inject AuthService and read
        // litrix_id at navigation time; falls back to / if it's missing.
        path: 'me',
        redirectTo: () => {
          const auth = inject(AuthService);
          const lid = auth.user()?.litrix_id;
          return lid ? `/profile/${lid}` : '/';
        },
      },
      {
        path: 'settings',
        component: SettingsComponent,
      },
      {
        path: 'search',
        component: SearchComponent,
      },
      {
        path: 'notifications',
        component: NotificationsComponent,
      },
    ],
  },

  { path: '**', redirectTo: '' },
];
