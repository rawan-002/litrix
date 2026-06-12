import {
  Component, inject, signal, computed, OnInit, HostListener,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterOutlet, RouterLink, RouterLinkActive, Router } from '@angular/router';
import { AuthService } from '../../core/services/auth.service';
import { NotificationsService } from '../../core/services/notifications.service';
import { LitrixApiService } from '../../services/litrix-api.service';
import { AffiliationService } from '../../core/services/affiliation.service';
import { IconComponent } from '../icon/icon.component';


interface NavItem {
  label: string;
  icon: string;
  route: string;
  permission?: string;
  // Optional badge count (e.g. pending reports) - a signal getter so the
  // template re-reads it without rebuilding the whole nav.
  badge?: () => number;
}


@Component({
  selector: 'app-layout',
  standalone: true,
  imports: [CommonModule, RouterOutlet, RouterLink, RouterLinkActive, IconComponent],
  templateUrl: './layout.component.html',
})
export class LayoutComponent implements OnInit {
  protected auth = inject(AuthService);
  protected notifs = inject(NotificationsService);
  protected affiliation = inject(AffiliationService);
  private api = inject(LitrixApiService);
  private router = inject(Router);

  readonly userMenuOpen = signal(false);

  // Mobile drawer state. The sidebar is a static column on lg+ screens and an
  // off-canvas drawer below that; this only drives the small-screen overlay.
  readonly sidebarOpen = signal(false);

  // Unfinished campaign submissions (pending / in_progress / reopened),
  // shown as the badge on "My Reports". Loaded once on init; the
  // my-reports page calls reloadPendingReports() after a submit.
  readonly pendingReports = signal(0);

  ngOnInit() {
    this.notifs.load(true).subscribe({ error: () => {} });
    this.reloadPendingReports();
    // Refresh the cached user so fields added after their last login (e.g.
    // photo_url) populate without forcing a re-login. Silent on failure -
    // the stored session stays valid.
    this.auth.fetchMe().subscribe({ error: () => {} });
  }

  reloadPendingReports() {
    this.api.getMyReports().subscribe({
      next: r => this.pendingReports.set(r.pending_count ?? 0),
      error: () => this.pendingReports.set(0),
    });
  }

  // Cmd/Ctrl+K jumps to search - the familiar Spotlight/Slack shortcut.
  @HostListener('document:keydown', ['$event'])
  handleKeydown(ev: KeyboardEvent) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') {
      ev.preventDefault();
      this.router.navigate(['/search']);
    }
  }

  readonly navItems = computed<NavItem[]>(() => {
    const items: NavItem[] = [
      { label: 'Dashboard',     icon: 'home',   route: '/' },
      { label: 'Search',        icon: 'search', route: '/search' },

      // Always present; the badge surfaces pending work, and the page
      // itself is an empty state when no campaigns are active.
      {
        label: 'My Reports',
        icon:  'file-text',
        route: '/my-reports',
        badge: () => this.pendingReports(),
      },
    ];

    // Shown to anyone with a "view researchers" perm (Admin/Dean/HoD);
    // the page scopes the data by role itself.
    if (this.auth.hasPermission('view_all_researchers') ||
        this.auth.hasPermission('view_dept_researchers') ||
        this.auth.hasPermission('manage_departments')) {
      items.push({ label: 'Departments', icon: 'building', route: '/departments' });
    }

    // Open to everyone; route stays /network even though the label is
    // now "Collaboration" (renaming the route wasn't worth the churn).
    items.push({ label: 'Collaboration', icon: 'share', route: '/network' });

    // Up top so it reads as a first-class feature (RAG wiring lands later).
    items.push({ label: 'Litrix AI', icon: 'sparkles', route: '/ai' });

    if (this.auth.hasPermission('approve_registrations')) {
      items.push({ label: 'Registrations', icon: 'user-check', route: '/admin/registrations' });
    }

    if (this.auth.hasPermission('manage_users')) {
      items.push({ label: 'Users', icon: 'users', route: '/admin/users' });
      items.push({ label: 'Invitations', icon: 'mail', route: '/admin/invitations' });
    }

    if (this.auth.hasPermission('manage_roles')) {
      items.push({ label: 'Roles & Permissions', icon: 'shield-check', route: '/admin/roles' });
    }

    // For campaign managers and HoD-style read access. Route stays
    // /admin/campaigns; only the label needed to read as "official".
    if (this.auth.hasPermission('manage_campaigns') ||
        this.auth.hasPermission('view_campaign_reports')) {
      items.push({ label: 'Research Reports', icon: 'clipboard-list', route: '/admin/campaigns' });
    }

    if (this.auth.hasPermission('trigger_sync')) {
      items.push({ label: 'Sync Control', icon: 'refresh', route: '/admin/sync' });
    }

    if (this.auth.hasPermission('view_audit_log')) {
      items.push({ label: 'Audit Log', icon: 'scroll', route: '/admin/audit' });
    }

    items.push({ label: 'My Profile',     icon: 'user', route: '/me' });
    items.push({ label: 'Notifications',  icon: 'bell', route: '/notifications' });

    return items;
  });

  readonly initials = computed(() => {
    const name = this.auth.user()?.full_name || this.auth.user()?.email || '';
    return name.split(' ').slice(0, 2).map(s => s[0] || '').join('').toUpperCase() || '?';
  });

  toggleSidebar() {
    this.sidebarOpen.update(v => !v);
  }

  closeSidebar() {
    this.sidebarOpen.set(false);
  }

  toggleUserMenu() {
    this.userMenuOpen.update(v => !v);
  }

  closeUserMenu() {
    this.userMenuOpen.set(false);
  }

  logout() {
    this.userMenuOpen.set(false);
    this.auth.logout();
  }
}
