import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { AuthService } from '../../core/services/auth.service';


type Step = 'request' | 'reset' | 'done';


@Component({
  selector: 'app-forgot-password',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, RouterLink],
  templateUrl: './forgot-password.component.html',
})
export class ForgotPasswordComponent {
  private fb = inject(FormBuilder);
  private auth = inject(AuthService);

  readonly step = signal<Step>('request');
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly email = signal('');

  readonly requestForm = this.fb.nonNullable.group({
    email: ['', [Validators.required, Validators.email]],
  });

  readonly resetForm = this.fb.nonNullable.group({
    token: ['', [Validators.required, Validators.minLength(6)]],
    new_password: ['', [Validators.required, Validators.minLength(8)]],
  });

  submitRequest() {
    if (this.requestForm.invalid) return;
    this.loading.set(true);
    this.error.set(null);

    const email = this.requestForm.getRawValue().email;
    this.auth.passwordResetRequest(email).subscribe({
      next: () => {
        this.email.set(email);
        this.step.set('reset');
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Something went wrong. Try again.');
        this.loading.set(false);
      },
    });
  }

  submitReset() {
    if (this.resetForm.invalid) return;
    this.loading.set(true);
    this.error.set(null);

    const { token, new_password } = this.resetForm.getRawValue();
    this.auth.passwordResetConfirm(this.email(), token, new_password).subscribe({
      next: () => {
        this.step.set('done');
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err.error?.error || 'Invalid code or expired');
        this.loading.set(false);
      },
    });
  }
}
