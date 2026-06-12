// One settings page for every role - name, password, and academic IDs are the
// same primitives for everyone, so splitting per-role would just duplicate
// logic. Non-researchers can leave the Academic IDs block blank.
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

  // --- Profile photo ---------------------------------------------------
  // Live preview of the chosen photo (data URI) before/after save; null
  // means "fall back to the stored photo or initials".
  readonly photoPreview = signal<string | null>(null);
  readonly photoSaving = signal(false);
  readonly photoError = signal<string | null>(null);

  // What the avatar should show: a freshly-picked preview wins, else the
  // user's stored photo_url, else null (initials).
  readonly currentPhoto = signal<string | null>(this.auth.user()?.photo_url ?? null);

  // Hides the first-run helper subtitle once any profile data exists.
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

  // Shown to everyone for now so dual-roled users can still claim a
  // Scholar/ORCID profile; can be gated by role later if needed.
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

  get initials(): string {
    const name = this.auth.user()?.full_name || this.auth.user()?.email || '';
    return name.split(/\s+/).slice(0, 2).map(s => s[0] || '').join('').toUpperCase() || '?';
  }

  // Read the picked file, downscale it to a 256x256 square JPEG (center-crop)
  // and keep it as a data URI. Resizing client-side keeps the stored string
  // tiny (~30-50 KB) so it fits comfortably in the PhotoURL text column with
  // no object storage. Rejects non-images up front.
  onPhotoSelected(ev: Event) {
    this.photoError.set(null);
    const input = ev.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      this.photoError.set('Please choose an image file');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const SIZE = 256;
        const canvas = document.createElement('canvas');
        canvas.width = SIZE;
        canvas.height = SIZE;
        const ctx = canvas.getContext('2d');
        if (!ctx) { this.photoError.set('Could not process image'); return; }
        // Center-crop the shorter side, then scale to fill the square.
        const side = Math.min(img.width, img.height);
        const sx = (img.width - side) / 2;
        const sy = (img.height - side) / 2;
        ctx.drawImage(img, sx, sy, side, side, 0, 0, SIZE, SIZE);
        this.photoPreview.set(canvas.toDataURL('image/jpeg', 0.85));
        input.value = '';   // allow re-picking the same file
      };
      img.onerror = () => this.photoError.set('Could not read image');
      img.src = reader.result as string;
    };
    reader.onerror = () => this.photoError.set('Could not read file');
    reader.readAsDataURL(file);
  }

  savePhoto() {
    const photo = this.photoPreview();
    if (!photo) return;
    this.photoSaving.set(true);
    this.photoError.set(null);
    this.http.patch(`${environment.apiBaseUrl}/auth/me/`, { photo_url: photo })
      .subscribe({
        next: (user: any) => this.afterPhotoSaved(user, 'Photo updated'),
        error: err => {
          this.photoError.set(err.error?.error || 'Failed to update photo');
          this.photoSaving.set(false);
        },
      });
  }

  removePhoto() {
    this.photoSaving.set(true);
    this.photoError.set(null);
    this.http.patch(`${environment.apiBaseUrl}/auth/me/`, { photo_url: '' })
      .subscribe({
        next: (user: any) => this.afterPhotoSaved(user, 'Photo removed'),
        error: err => {
          this.photoError.set(err.error?.error || 'Failed to remove photo');
          this.photoSaving.set(false);
        },
      });
  }

  private afterPhotoSaved(user: any, msg: string) {
    localStorage.setItem('litrix_user', JSON.stringify(user));
    this.auth.user.set(user);
    this.currentPhoto.set(user.photo_url ?? null);
    this.photoPreview.set(null);
    this.success.set(msg);
    this.photoSaving.set(false);
    setTimeout(() => this.success.set(null), 3000);
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
