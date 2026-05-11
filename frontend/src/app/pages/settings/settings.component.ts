/**
 * Settings page — centralized data-update hub for all roles.
 *
 * Why a single page for all roles?
 *   Every user (Admin, Dean, HoD, Researcher) needs the same primitives:
 *   change name (Arabic), change password. Researchers additionally need
 *   academic IDs for sync. Splitting per-role would duplicate logic and
 *   harm consistency.
 *
 * The Academic IDs block is shown for everyone — non-researchers can
 * simply leave it blank. We can later gate it on permission if needed.
 */
import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { AuthService } from '../../core/services/auth.service';
import { environment } from '../../../environments/environment';


@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule],
  templateUrl: './settings.component.html',
})
export class SettingsComponent {
  private fb = inject(FormBuilder);
  private http = inject(HttpClient);
  protected auth = inject(AuthService);

  readonly saving = signal(false);
  readonly success = signal<string | null>(null);
  readonly error = signal<string | null>(null);

  // First-run hint flag — hides the helper subtitle once profile data
  // exists. Seeded from existing AuthUser fields.
  readonly everSaved = signal<boolean>(this.hasAnyProfileData());

  readonly profileForm = this.fb.nonNullable.group({
    full_name_ar: [this.auth.user()?.full_name_ar || ''],
    scholar_id:   [this.auth.user()?.scholar_id || ''],
    orcid_id:     [this.auth.user()?.orcid_id || ''],
    scopus_id:    [this.auth.user()?.scopus_id || ''],
  });

  readonly passwordForm = this.fb.nonNullable.group({
    old_password: ['', Validators.required],
    new_password: ['', [Validators.required, Validators.minLength(8)]],
  });

  readonly passwordSaving = signal(false);
  readonly passwordSuccess = signal<string | null>(null);
  readonly passwordError = signal<string | null>(null);

  // Researchers benefit from the Academic IDs block. We still expose it
  // to others so they can claim a Scholar/ORCID profile if they're
  // dual-roled, but the section can be hidden by role if desired later.
  readonly showAcademicIds = true;

  private hasAnyProfileData(): boolean {
    const u = this.auth.user();
    if (!u) return false;
    return !!(u.full_name_ar || u.scholar_id || u.orcid_id || u.scopus_id);
  }

  saveProfile() {
    this.saving.set(true);
    this.success.set(null);
    this.error.set(null);
    this.http.patch(`${environment.apiBaseUrl}/auth/me/`, this.profileForm.getRawValue())
      .subscribe({
        next: (user: any) => {
          localStorage.setItem('litrix_user', JSON.stringify(user));
          this.auth.user.set(user);
          this.success.set('Profile updated successfully');
          this.everSaved.set(true);
          this.saving.set(false);
          setTimeout(() => this.success.set(null), 3000);
        },
        error: err => {
          this.error.set(err.error?.error || 'Failed to update profile');
          this.saving.set(false);
        },
      });
  }

  changePassword() {
    if (this.passwordForm.invalid) return;
    this.passwordSaving.set(true);
    this.passwordSuccess.set(null);
    this.passwordError.set(null);
    this.http.post(
      `${environment.apiBaseUrl}/auth/change-password/`,
      this.passwordForm.getRawValue(),
    ).subscribe({
      next: () => {
        this.passwordForm.reset();
        this.passwordSuccess.set('Password changed successfully');
        this.passwordSaving.set(false);
        setTimeout(() => this.passwordSuccess.set(null), 3000);
      },
      error: err => {
        this.passwordError.set(err.error?.error || 'Failed to change password');
        this.passwordSaving.set(false);
      },
    });
  }
}
