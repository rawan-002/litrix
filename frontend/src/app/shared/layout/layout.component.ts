import {
  Component, inject, signal, computed, OnInit, HostListener,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterOutlet, RouterLink, RouterLinkActive, Router } from '@angular/router';
import { AuthService } from '../../core/services/auth.service';
import { NotificationsService } from '../../core/services/notifications.service';


interface NavItem {
  label: string;
  icon: string;
  route: string;
  permission?: string;
}


@Component({
  selector: 'app-layout',
  standalone: true,
  imports: [CommonModule, RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './layout.component.html',
})
export class LayoutComponent implements OnInit {
  protected auth = inject(AuthService);
  protected notifs = inject(NotificationsService);
  private router = inject(Router);

  readonly userMenuOpen = signal(false);

  ngOnInit() {
    this.notifs.load(true).subscribe({ error: () => {} });
  }

  /**
   * Cmd+K (mac) / Ctrl+K (win/linux) — keyboard shortcut to the search
   * page. Standard Spotlight/Slack/Notion convention.
   */
  @HostListener('document:keydown', ['$event'])
  handleKeydown(ev: KeyboardEvent) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') {
      ev.preventDefault();
      this.router.navigate(['/search']);
    }
  }

  readonly navItems = computed<NavItem[]>(() => {
    const items: NavItem[] = [
      { label: 'Dashboard',     icon: '⌂',  route: '/' },
      { label: 'Search',        icon: '⌕',  route: '/search' },
    ];

    if (this.auth.hasPermission('view_all_researchers') ||
        this.auth.hasPermission('view_dept_researchers')) {
      items.push({ label: 'Researchers', icon: '◉', route: '/researchers' });
    }

    if (this.auth.hasPermission('manage_departments')) {
      items.push({ label: 'Departments', icon: '◫', route: '/departments' });
    }

    if (this.auth.hasPermission('approve_registrations')) {
      items.push({ label: 'Registrations', icon: '✓', route: '/admin/registrations' });
    }

    if (this.auth.hasPermission('manage_users')) {
      items.push({ label: 'Users', icon: '◐', route: '/admin/users' });
      items.push({ label: 'Invitations', icon: '✉', route: '/admin/invitations' });
    }

    if (this.auth.hasPermission('manage_roles')) {
      items.push({ label: 'Roles & Permissions', icon: '◈', route: '/admin/roles' });
    }

    if (this.auth.hasPermission('trigger_sync')) {
      items.push({ label: 'Sync Control', icon: '↻', route: '/admin/sync' });
    }

    if (this.auth.hasPermission('view_audit_log')) {
      items.push({ label: 'Audit Log', icon: '☰', route: '/admin/audit' });
    }

    items.push({ label: 'My Profile',     icon: '◔', route: '/me' });
    items.push({ label: 'Notifications',  icon: '◇', route: '/notifications' });

    return items;
  });

  readonly initials = computed(() => {
    const name = this.auth.user()?.full_name || this.auth.user()?.email || '';
    return name.split(' ').slice(0, 2).map(s => s[0] || '').join('').toUpperCase() || '?';
  });

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
