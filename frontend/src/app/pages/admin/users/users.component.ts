import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface User {
  UserID: number;
  Email: string;
  FullName_Ar: string | null;
  FirstName: string;
  MiddleName: string | null;
  LastName: string;
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

/**
 * Editable form model. Mirrors the whitelist on the backend
 * (update_user view). We never let the form bind to UserID,
 * Litrix_ID, PasswordHash, Scholar_ID — those need a different path.
 */
interface EditForm {
  email:          string;
  full_name_ar:   string;
  first_name:     string;
  middle_name:    string;
  last_name:      string;
  user_type:      string;
  account_status: string;
  is_active:      boolean;
  role_id:        number | null;
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

  /** User being edited via the modal. null = modal closed. */
  readonly editingUser = signal<User | null>(null);
  /** Mutable form state — bound to the modal inputs. */
  readonly editForm   = signal<EditForm | null>(null);
  /** Toggles the "Saving…" state on the modal's Save button. */
  readonly savingEdit = signal(false);

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

  // ----------------------------------------------------------------
  // Edit flow — open modal with a clone of the user, save via PATCH.
  // We do NOT mutate the table row in place. Only on a successful
  // PATCH do we reload, so a failed save doesn't leave the UI lying.
  // ----------------------------------------------------------------

  askEdit(user: User) {
    this.editingUser.set(user);
    this.editForm.set({
      email:          user.Email || '',
      full_name_ar:   user.FullName_Ar || '',
      first_name:     user.FirstName || '',
      middle_name:    user.MiddleName || '',
      last_name:      user.LastName || '',
      user_type:      user.UserType || 'Researcher',
      account_status: user.AccountStatus || 'Active',
      is_active:      !!user.IsActive,
      role_id:        user.RoleID,
    });
  }

  cancelEdit() {
    this.editingUser.set(null);
    this.editForm.set(null);
  }

  saveEdit() {
    const u = this.editingUser();
    const f = this.editForm();
    if (!u || !f) return;

    // Only send changed fields — keeps the audit log clean and lets the
    // backend skip rows that aren't actually being mutated.
    const payload: any = {};
    if (f.email          !== (u.Email          || ''))  payload.email          = f.email.trim();
    if (f.full_name_ar   !== (u.FullName_Ar    || ''))  payload.full_name_ar   = f.full_name_ar.trim();
    if (f.first_name     !== (u.FirstName      || ''))  payload.first_name     = f.first_name.trim();
    if (f.middle_name    !== (u.MiddleName     || ''))  payload.middle_name    = f.middle_name.trim();
    if (f.last_name      !== (u.LastName       || ''))  payload.last_name      = f.last_name.trim();
    if (f.user_type      !== u.UserType)                payload.user_type      = f.user_type;
    if (f.account_status !== u.AccountStatus)           payload.account_status = f.account_status;
    if (f.is_active      !== u.IsActive)                payload.is_active      = f.is_active;
    if (f.role_id        !== u.RoleID)                  payload.role_id        = f.role_id;

    if (Object.keys(payload).length === 0) {
      this.cancelEdit();
      return;
    }

    this.savingEdit.set(true);
    this.http.patch(`${this.API}/users/${u.UserID}/`, payload).subscribe({
      next: () => {
        this.savingEdit.set(false);
        this.cancelEdit();
        this.load();
      },
      error: err => {
        this.savingEdit.set(false);
        alert(err?.error?.error || 'Failed to save changes');
      },
    });
  }

  /**
   * Tiny helper to set one field on the form signal — keeps the
   * template's (ngModelChange) one-liners readable.
   */
  patchForm<K extends keyof EditForm>(key: K, value: EditForm[K]) {
    const f = this.editForm();
    if (!f) return;
    this.editForm.set({ ...f, [key]: value });
  }
}
