import { Component, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface Role {
  RoleID: number;
  Name: string;
  Description: string;
  IsSystem: boolean;
}

interface Permission {
  PermissionID: number;
  Code: string;
  Description: string;
  Category: string;
}


@Component({
  selector: 'app-roles',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './roles.component.html',
})
export class RolesComponent {
  private http = inject(HttpClient);
  private API = `${environment.apiBaseUrl}/auth`;

  readonly roles = signal<Role[]>([]);
  readonly permissions = signal<Permission[]>([]);
  readonly selectedRoleId = signal<number | null>(null);
  readonly rolePermIds = signal<Set<number>>(new Set());
  readonly loading = signal(true);
  readonly saving = signal(false);
  readonly newRoleName = signal('');
  readonly creating = signal(false);

  readonly permissionsByCategory = computed(() => {
    const grouped: { [k: string]: Permission[] } = {};
    for (const p of this.permissions()) {
      const cat = p.Category || 'other';
      grouped[cat] = grouped[cat] || [];
      grouped[cat].push(p);
    }
    return grouped;
  });

  readonly selectedRole = computed(() =>
    this.roles().find(r => r.RoleID === this.selectedRoleId()) || null,
  );

  constructor() {
    this.loadRoles();
    this.loadPermissions();
  }

  loadRoles() {
    this.loading.set(true);
    this.http.get<{ roles: Role[] }>(`${this.API}/roles/`).subscribe({
      next: r => {
        this.roles.set(r.roles || []);
        if (!this.selectedRoleId() && r.roles?.length) {
          this.selectRole(r.roles[0].RoleID);
        }
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  loadPermissions() {
    this.http.get<{ permissions: Permission[] }>(`${this.API}/permissions/`)
      .subscribe(r => this.permissions.set(r.permissions || []));
  }

  selectRole(id: number) {
    this.selectedRoleId.set(id);
    this.http.get<{ permission_ids: number[] }>(`${this.API}/roles/${id}/permissions/`)
      .subscribe(r => this.rolePermIds.set(new Set(r.permission_ids || [])));
  }

  togglePerm(pid: number) {
    this.rolePermIds.update(s => {
      const next = new Set(s);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  }

  hasPermSelected(pid: number): boolean {
    return this.rolePermIds().has(pid);
  }

  save() {
    const id = this.selectedRoleId();
    if (!id) return;
    this.saving.set(true);
    this.http.put(`${this.API}/roles/${id}/set-permissions/`, {
      permission_ids: Array.from(this.rolePermIds()),
    }).subscribe({
      next: () => this.saving.set(false),
      error: () => this.saving.set(false),
    });
  }

  createRole() {
    if (!this.newRoleName().trim()) return;
    this.creating.set(true);
    this.http.post<{ role_id: number }>(`${this.API}/roles/create/`, {
      name: this.newRoleName().trim(),
    }).subscribe({
      next: r => {
        this.creating.set(false);
        this.newRoleName.set('');
        this.loadRoles();
        this.selectRole(r.role_id);
      },
      error: () => this.creating.set(false),
    });
  }

  deleteRole(id: number) {
    if (!confirm('Delete this role?')) return;
    this.http.delete(`${this.API}/roles/${id}/`).subscribe({
      next: () => {
        if (this.selectedRoleId() === id) this.selectedRoleId.set(null);
        this.loadRoles();
      },
    });
  }
}
