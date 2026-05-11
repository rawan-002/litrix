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

export const routes: Routes = [
  { path: 'login',            component: LoginComponent,          canActivate: [guestGuard] },
  { path: 'register',         component: RegisterComponent,       canActivate: [guestGuard] },
  { path: 'forgot-password',  component: ForgotPasswordComponent, canActivate: [guestGuard] },

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
