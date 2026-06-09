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
  // Public landing page - shown to anyone not signed in.
  // guestGuard bounces already-authenticated users straight to /.
  { path: 'welcome',          component: WelcomeComponent,        canActivate: [guestGuard] },

  { path: 'login',            component: LoginComponent,          canActivate: [guestGuard] },
  { path: 'register',         component: RegisterComponent,       canActivate: [guestGuard] },
  { path: 'forgot-password',  component: ForgotPasswordComponent, canActivate: [guestGuard] },

  // ---- PUBLIC SUPERVISOR DASHBOARD ---------------------------------------
  // No login required. Lazy-loaded standalone components, NO LayoutComponent
  // (so the public pages don't render the authenticated app shell/sidebar).
  // Backend endpoints live under /api/public/* and are AllowAny.
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
      // New canonical profile URL — uses Lit-NNNNNN public identifier.
      { path: 'profile/:litrixId', component: ResearcherProfileComponent },
      // Legacy URL kept temporarily so existing links don't 404.
      // The component handles both :litrixId and :id route params.
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
        // Admin campaigns dashboard — visible to anyone who can
        // manage campaigns OR view their reports.
        path: 'admin/campaigns',
        component: CampaignsComponent,
        canActivate: [permissionGuard('manage_campaigns', 'view_campaign_reports')],
      },
      // My Reports — permanent entry for every authenticated user.
      // The page renders a friendly empty state when there are no
      // active campaigns, so no extra guard needed beyond authGuard.
      { path: 'my-reports',         component: MyReportsComponent },
      { path: 'my-reports/:id',     component: MyReportDetailComponent },
      {
        path: 'departments',
        component: DepartmentsComponent,
        canActivate: [permissionGuard(
          'view_all_researchers', 'view_dept_researchers', 'manage_departments',
        )],
      },
      // Research Network - collaboration graph, accessible to all
      // authenticated users (each one centred on themselves by default).
      { path: 'network', component: NetworkComponent },

      // Litrix AI — chatbot surface (lazy: keeps the bundle lean until
      // visited; RAG backend wiring lands later).
      {
        path: 'ai',
        loadComponent: () =>
          import('./pages/ai/litrix-ai.component')
            .then(m => m.LitrixAiComponent),
      },
      {
        // Dynamic redirect to the user's own /profile/Lit-NNNNNN URL.
        // Why a function: Angular needs to read the current AuthUser at
        // navigation time, and `redirectTo` as a function lets us inject
        // services. Falls back to the dashboard when litrix_id is
        // unexpectedly missing (defensive — should never happen).
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
