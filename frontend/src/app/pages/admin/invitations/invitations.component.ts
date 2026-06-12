// Admin invitations: create role-scoped invites (HoD/Dean/Researcher) and list
// existing ones with their status. The link is shown right after creation so
// the admin can copy it by hand while email delivery is still being set up.
import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface Invitation {
  InvitationID: number;
  Token: string;
  InvitedEmail: string;
  IntendedUserType: string;
  role_name: string | null;
  DepartmentName: string | null;
  ExpiresAt: string;
  UsedAt: string | null;
  RevokedAt: string | null;
  CreatedAt: string;
  invited_by_name: string | null;
  used_by_email: string | null;
  status: 'pending' | 'used' | 'revoked' | 'expired';
}

interface Department {
  department_id: number;
  department_name: string;
}


@Component({
  selector: 'app-invitations',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './invitations.component.html',
})
export class InvitationsComponent {
  private http = inject(HttpClient);
  private API  = `${environment.apiBaseUrl}/auth`;

  readonly invitations = signal<Invitation[]>([]);
  readonly departments = signal<Department[]>([]);
  readonly loading     = signal<boolean>(true);
  readonly creating    = signal<boolean>(false);

  // Set after a successful create so the admin can copy the link.
  readonly justCreated = signal<{
    token: string; url: string; expires_at: string;
  } | null>(null);

  // Form state
  readonly formEmail = signal<string>('');
  readonly formRole  = signal<'HoD' | 'Dean' | 'Researcher'>('HoD');
  readonly formDept  = signal<number | null>(null);
  readonly formTtl   = signal<number>(14);

  constructor() {
    this.load();
    this.http.get<{ departments: Department[] }>(
      `${this.API}/departments-public/`,
    ).subscribe({
      next: r => this.departments.set(r.departments || []),
      error: () => this.departments.set([]),
    });
  }

  load() {
    this.loading.set(true);
    this.http.get<{ invitations: Invitation[] }>(`${this.API}/invitations/`).subscribe({
      next: r => {
        this.invitations.set(r.invitations || []);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  create() {
    const email = this.formEmail().trim().toLowerCase();
    if (!email) { alert('Email is required'); return; }
    this.creating.set(true);
    this.http.post<{
      invitation_id: number; token: string; expires_at: string; email_sent: boolean;
    }>(`${this.API}/invitations/`, {
      email,
      role: this.formRole(),
      department_id: this.formRole() === 'HoD' ? this.formDept() : null,
      ttl_days: this.formTtl(),
    }).subscribe({
      next: res => {
        this.creating.set(false);
        const url =
          `${window.location.origin}/register?invite=${encodeURIComponent(res.token)}`;
        this.justCreated.set({
          token: res.token, url, expires_at: res.expires_at,
        });
        this.formEmail.set('');
        this.load();
      },
      error: err => {
        this.creating.set(false);
        alert(err?.error?.error || 'Failed to create invitation');
      },
    });
  }

  revoke(inv: Invitation) {
    if (!confirm(`Revoke invitation for ${inv.InvitedEmail}?`)) return;
    this.http.delete(`${this.API}/invitations/${inv.InvitationID}/`).subscribe({
      next: () => this.load(),
      error: err => alert(err?.error?.error || 'Failed to revoke'),
    });
  }

  copyLink(token: string) {
    const url = `${window.location.origin}/register?invite=${encodeURIComponent(token)}`;
    navigator.clipboard.writeText(url).then(
      () => alert('Link copied to clipboard'),
      () => prompt('Copy this link:', url),
    );
  }

  dismissCreated() { this.justCreated.set(null); }

  statusBadge(s: Invitation['status']): string {
    return {
      pending: 'bg-amber-50 text-amber-700',
      used:    'bg-green-50 text-green-700',
      revoked: 'bg-ink-100 text-ink-500',
      expired: 'bg-red-50 text-red-700',
    }[s] || 'bg-ink-100 text-ink-500';
  }

  formatDate(s: string | null): string {
    if (!s) return '-';
    return new Date(s).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  }
}
