import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface User {
  UserID: number;
  Email: string;
  FullName_Ar: string | null;
  UserType: string;
  AccountStatus: string;
  IsActive: boolean;
  EmailVerified: boolean;
  Scholar_ID: string | null;
  Orcid_ID: string | null;
  Scopus_ID: string | null;
  LastLoginAt: string | null;
  CreatedAt: string;
  RoleID: number | null;
  role_name: string | null;
  // Pulled in via the LATERAL Works_In join so admins can see
  // a user's department at a glance — relevant when promoting
  // a Researcher to HoD of that exact department.
  departmentname: string | null;
}

interface Role {
  RoleID: number;
  Name: string;
  Description: string;
  IsSystem: boolean;
}


@Component({
  selector: 'app-users',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './users.component.html',
})
export class UsersComponent {
  private http = inject(HttpClient);
  private API = `${environment.apiBaseUrl}/auth`;

  readonly users = signal<User[]>([]);
  readonly roles = signal<Role[]>([]);
  readonly loading = signal(true);
  readonly search = signal('');
  readonly editingId = signal<number | null>(null);

  /** UserID currently being deleted (drives row spinner / disable). */
  readonly deletingId = signal<number | null>(null);

  /** Pending delete target for the confirm modal. null = modal closed. */
  readonly pendingDelete = signal<User | null>(null);

  readonly filtered = computed(() => {
    const q = this.search().toLowerCase();
    return q
      ? this.users().filter(u =>
          u.Email.toLowerCase().includes(q) ||
          (u.FullName_Ar || '').toLowerCase().includes(q))
      : this.users();
  });

  constructor() {
    this.load();
    this.http.get<{ roles: Role[] }>(`${this.API}/roles/`)
      .subscribe(r => this.roles.set(r.roles || []));
  }

  load() {
    this.loading.set(true);
    this.http.get<{ users: User[] }>(`${this.API}/users/`).subscribe({
      next: res => {
        this.users.set(res.users || []);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  toggleActive(user: User) {
    this.update(user.UserID, { is_active: !user.IsActive });
  }

  changeRole(user: User, roleId: number) {
    this.update(user.UserID, { role_id: roleId });
  }

  private update(userId: number, payload: any) {
    this.http.patch(`${this.API}/users/${userId}/`, payload).subscribe({
      next: () => this.load(),
    });
  }

  formatDate(s: string | null): string {
    if (!s) return '—';
    return new Date(s).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  }

  // ----------------------------------------------------------------
  // Delete flow — two-step: open confirmation modal, then commit.
  // ----------------------------------------------------------------

  askDelete(user: User) { this.pendingDelete.set(user); }
  cancelDelete()        { this.pendingDelete.set(null); }

  confirmDelete() {
    const u = this.pendingDelete();
    if (!u) return;
    this.deletingId.set(u.UserID);
    this.http.delete<{ message: string; deleted: any }>(
      `${this.API}/users/${u.UserID}/`,
    ).subscribe({
      next: () => {
        // Optimistic local removal so the UI feels snappy.
        this.users.update(list => list.filter(x => x.UserID !== u.UserID));
        this.deletingId.set(null);
        this.pendingDelete.set(null);
      },
      error: err => {
        this.deletingId.set(null);
        alert(err?.error?.error || 'Failed to delete user');
      },
    });
  }
}
