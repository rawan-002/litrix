import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';
import { isLatinName, joinEnglishName } from '../../../shared/utils/researcher-name';


interface User {
  UserID: number;
  Email: string;
  FullName_Ar: string | null;
  ScholarDisplayName: string | null;
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
  // From the LATERAL Works_In join - lets admins see a user's department at a
  // glance, which matters when promoting a Researcher to HoD of it.
  departmentname: string | null;
}

// Editable fields, mirroring the backend whitelist in update_user. UserID,
// Litrix_ID, PasswordHash, and Scholar_ID are deliberately not here - they go
// through different paths.
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

  displayName(u: User): string {
    const fallbackEn = joinEnglishName(u.FirstName, u.LastName);
    const en = u.ScholarDisplayName || fallbackEn;
    return (isLatinName(en) ? en : '') || u.Email;
  }

  // Row currently being deleted - drives its spinner / disabled state.
  readonly deletingId = signal<number | null>(null);

  // Delete-confirm target; null means the modal is closed.
  readonly pendingDelete = signal<User | null>(null);

  // Edit-modal state; null means closed.
  readonly editingUser = signal<User | null>(null);
  readonly editForm   = signal<EditForm | null>(null);
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
      .subscribe({
        next: r => this.roles.set(r.roles || []),
        error: () => this.roles.set([]),
      });
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
      // Re-sync on failure so the row doesn't keep an optimistic, wrong value.
      error: (err) => { alert(err?.error?.error || 'Failed to update user'); this.load(); },
    });
  }

  formatDate(s: string | null): string {
    if (!s) return '-';
    return new Date(s).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  }

  // Delete is two-step: open the confirm modal, then commit.
  askDelete(user: User) { this.pendingDelete.set(user); }
  cancelDelete()        { this.pendingDelete.set(null); }

  confirmDelete() {
    const u = this.pendingDelete();
    if (!u) return;
    this.deletingId.set(u.UserID);
    this.http.delete<{ message: string; deleted?: any; unregistered?: any }>(
      `${this.API}/users/${u.UserID}/`,
    ).subscribe({
      next: (res) => {
        // Drop the row locally so it feels instant.
        this.users.update(list => list.filter(x => x.UserID !== u.UserID));
        this.deletingId.set(null);
        this.pendingDelete.set(null);
        // Tell the admin when the user was un-registered (papers kept) vs deleted.
        if (res?.unregistered) {
          alert(res.message);
        }
      },
      error: err => {
        this.deletingId.set(null);
        alert(err?.error?.error || 'Failed to delete user');
      },
    });
  }

  // Edit opens the modal on a clone and saves via PATCH; the row only reloads
  // on success, so a failed save never leaves the table showing wrong data.
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

    // Send only changed fields so the audit log stays clean.
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

  // Set one field on the form signal - keeps the template's (ngModelChange)
  // one-liners tidy.
  patchForm<K extends keyof EditForm>(key: K, value: EditForm[K]) {
    const f = this.editForm();
    if (!f) return;
    this.editForm.set({ ...f, [key]: value });
  }
}
