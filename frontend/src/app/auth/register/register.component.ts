import { Component, inject, signal, computed, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink, ActivatedRoute } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { Subject, debounceTime, switchMap, of } from 'rxjs';
import { AuthService } from '../../core/services/auth.service';
import { environment } from '../../../environments/environment';

interface InvitePayload {
  valid: boolean;
  reason?: string;
  invited_email?: string;
  intended_user_type?: string;
  intended_role?: string;
  department_id?: number | null;
  department_name?: string | null;
  expires_at?: string;
}


type Step = 'profile' | 'verify' | 'done';

interface VerificationMismatch {
  field: string;
  label: string;
  your_value: any;
  our_value: any;
  our_label?: string;
  // Drives the banner color: high = red, info = blue, default amber.
  severity?: 'high' | 'info';
  note?: string;
}

interface VerificationResult {
  match_found: boolean;
  matched_by: 'scholar_id' | 'orcid_id' | 'email' | null;
  stored: {
    full_name_ar: string | null;
    department_id: number | null;
    department_name: string | null;
    academic_rank: string | null;
    papers_count: number;
  } | null;
  mismatches: VerificationMismatch[];
}


@Component({
  selector: 'app-register',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, RouterLink],
  templateUrl: './register.component.html',
})
export class RegisterComponent implements OnInit, OnDestroy {
  private fb = inject(FormBuilder);
  private auth = inject(AuthService);
  private http = inject(HttpClient);
  private router = inject(Router);
  private route = inject(ActivatedRoute);

  readonly step = signal<Step>('profile');
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly departments = signal<{ department_id: number; department_name: string }[]>([]);

  /** Invitation state — populated when ?invite=<token> is in the URL. */
  readonly inviteToken = signal<string | null>(null);
  readonly invite      = signal<InvitePayload | null>(null);

  // Live result of the debounced /registration-match/ check.
  readonly verification = signal<VerificationResult | null>(null);
  readonly verifying    = signal<boolean>(false);

  /** Severity bucket used by the UI to pick banner color. */
  readonly highestSeverity = computed<'high' | 'medium' | 'info' | null>(() => {
    const v = this.verification();
    if (!v?.mismatches?.length) return null;
    const sevs = v.mismatches.map(m => m.severity || 'medium');
    if (sevs.includes('high')) return 'high';
    if (sevs.some(s => s !== 'info')) return 'medium';
    return 'info';
  });

  // Drives the debounced lookup pipeline.
  private matchPing$ = new Subject<void>();

  // Splitting the English name maps straight onto the User table's
  // FirstName/LastName columns and dodges the lossy split-on-space in
  // approve_registration. Arabic stays one field (FullName_Ar today), but
  // the split form means no rework if we ever add Arabic name columns.
  readonly profileForm = this.fb.nonNullable.group({
    email:           ['', [Validators.required, Validators.email]],
    password:        ['', [Validators.required, Validators.minLength(8)]],
    // Required — the canonical name for citation lookups and reporting.
    first_name_en:   ['', [Validators.required]],
    middle_name_en:  [''],
    last_name_en:    ['', [Validators.required]],
    // Optional single triple-name field (الاسم الثلاثي) -> FullName_Ar;
    // scraper-imported researchers may not have an Arabic name yet.
    full_name_ar:    [''],
    department_id:   [null as number | null],
    academic_rank:   [''],
    scholar_id:      [''],
    orcid_id:        [''],
  });

  // Pull the bare Scholar ID out of a pasted profile URL (it's in the
  // `user=` param). Scholar_ID is VARCHAR(64), so a full URL would overflow.
  private extractScholarId(raw: string | null | undefined): string {
    const s = (raw || '').trim();
    if (!s) return '';
    const m = s.match(/[?&]user=([^&#]+)/i);
    return m ? m[1] : s;
  }

  // Pull the bare ORCID out of a full orcid.org URL (or pass it through).
  private extractOrcid(raw: string | null | undefined): string {
    const s = (raw || '').trim();
    if (!s) return '';
    const m = s.match(/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])/i);
    return m ? m[1].toUpperCase() : s;
  }

  /** Concatenated full names — what the backend currently expects. */
  private buildFullNames() {
    const v = this.profileForm.getRawValue();
    const fullAr = (v.full_name_ar || '').trim();
    const fullEn = [v.first_name_en, v.middle_name_en, v.last_name_en]
      .map(s => (s || '').trim()).filter(Boolean).join(' ');
    return { full_name_ar: fullAr, full_name_en: fullEn };
  }

  readonly verifyForm = this.fb.nonNullable.group({
    token: ['', [Validators.required, Validators.minLength(6)]],
  });

  readonly registeredEmail = signal<string>('');
  // False means the verification email failed to send — we warn and
  // offer a resend.
  readonly emailSent   = signal<boolean>(true);
  readonly resending   = signal<boolean>(false);
  readonly resendNote  = signal<string | null>(null);

  isStepDone(s: string, i: number): boolean {
    const order = ['profile', 'verify', 'done'];
    return order.indexOf(this.step()) > i;
  }

  constructor() {
    this.http.get<{ departments?: any[] } | any[]>(`${environment.apiBaseUrl}/auth/departments-public/`)
      .subscribe({
        next: (res: any) => {
          const list = res.departments || res || [];
          this.departments.set(list);
        },
        error: () => this.departments.set([]),
      });
  }

  ngOnInit() {
    // Read the invite token up front so we can lock email/department
    // before the user starts typing.
    const t = this.route.snapshot.queryParamMap.get('invite');
    if (t) {
      this.inviteToken.set(t);
      this.http.get<InvitePayload>(
        `${environment.apiBaseUrl}/auth/invitations/lookup/${encodeURIComponent(t)}/`,
      ).subscribe({
        next: payload => {
          this.invite.set(payload);
          if (payload.valid) {
            // Pre-fill and lock whatever the invitation already pinned.
            const patch: any = {};
            if (payload.invited_email) patch.email = payload.invited_email;
            if (payload.department_id) patch.department_id = payload.department_id;
            this.profileForm.patchValue(patch);
            // Email is bound to the invite — lock it so it can't drift and
            // trigger a mismatch at submit.
            this.profileForm.get('email')?.disable();
            if (payload.department_id) {
              this.profileForm.get('department_id')?.disable();
            }
          }
        },
        error: () => this.invite.set({ valid: false, reason: 'lookup_failed' }),
      });
    }

    // Debounced identity check — re-runs on every relevant field change
    // so the warning stays in sync with the form.
    this.matchPing$
      .pipe(
        debounceTime(400),
        switchMap(() => {
          const v = this.profileForm.getRawValue();
          // Nothing to match without at least one identity key.
          if (!v.scholar_id && !v.orcid_id && !v.email) {
            return of(null);
          }
          this.verifying.set(true);
          const names = this.buildFullNames();
          return this.http.post<VerificationResult>(
            `${environment.apiBaseUrl}/auth/registration-match/`,
            {
              scholar_id:    this.extractScholarId(v.scholar_id) || null,
              orcid_id:      this.extractOrcid(v.orcid_id) || null,
              email:         v.email || null,
              department_id: v.department_id,
              academic_rank: v.academic_rank || null,
              full_name_ar:  names.full_name_ar || null,
            },
          );
        }),
      )
      .subscribe({
        next: res => {
          this.verifying.set(false);
          this.verification.set(res);
        },
        error: () => {
          this.verifying.set(false);
          this.verification.set(null);
        },
      });

    this.profileForm.valueChanges.subscribe(() => this.matchPing$.next());
  }

  ngOnDestroy() {
    this.matchPing$.complete();
  }

  submitProfile() {
    if (this.profileForm.invalid) return;
    this.loading.set(true);
    this.error.set(null);

    const v = this.profileForm.getRawValue();
    const names = this.buildFullNames();
    // Strip pasted URLs down to bare IDs before they hit the VARCHAR(64)
    // columns, and reflect the cleaned values back in the form.
    const scholar_id = this.extractScholarId(v.scholar_id);
    const orcid_id   = this.extractOrcid(v.orcid_id);
    this.profileForm.patchValue({ scholar_id, orcid_id }, { emitEvent: false });
    const payload: any = {
      email:         v.email,
      password:      v.password,
      full_name_ar:  names.full_name_ar,
      full_name_en:  names.full_name_en,
      first_name:    (v.first_name_en  || '').trim(),
      middle_name:   (v.middle_name_en || '').trim(),
      last_name:     (v.last_name_en   || '').trim(),
      department_id: v.department_id,
      academic_rank: v.academic_rank,
      scholar_id:    scholar_id,
      orcid_id:      orcid_id,
    };
    // Pass the invite token through — the backend uses it to skip the
    // email-verification queue.
    if (this.inviteToken()) {
      payload.invite = this.inviteToken();
    }
    this.auth.register(payload).subscribe({
      next: (res: any) => {
        this.loading.set(false);
        // Invite flow returns a user_id and skips email verification —
        // jump straight to the done step.
        if (this.inviteToken() && res?.user_id) {
          this.step.set('done');
          return;
        }
        this.registeredEmail.set(payload.email);
        this.emailSent.set(res?.email_sent !== false);
        this.step.set('verify');
      },
      error: err => {
        this.loading.set(false);
        // 409 from the identity gate — reuse the live-check banner and
        // keep the user on the profile step to fix it.
        if (err.status === 409 && err.error?.error === 'identity_verification_failed') {
          this.verification.set(err.error.verification);
          this.error.set(
            err.error.message ||
            'Your registration conflicts with our records.',
          );
          return;
        }
        const detail = err.error || {};
        this.error.set(
          Object.values(detail).flat().join(' · ') || 'Registration failed'
        );
      },
    });
  }

  submitVerify() {
    if (this.verifyForm.invalid) return;
    this.loading.set(true);
    this.error.set(null);

    this.auth.verifyEmail(
      this.registeredEmail(),
      this.verifyForm.getRawValue().token,
    ).subscribe({
      next: () => {
        this.loading.set(false);
        this.step.set('done');
      },
      error: err => {
        this.loading.set(false);
        this.error.set(err.error?.error || 'Invalid verification code');
      },
    });
  }

  /** Re-send the verification code (delivery failed, lost, or spam-foldered). */
  resend() {
    if (this.resending()) return;
    this.resending.set(true);
    this.resendNote.set(null);
    this.auth.resendVerification(this.registeredEmail()).subscribe({
      next: (res: any) => {
        this.resending.set(false);
        this.emailSent.set(res?.email_sent !== false);
        this.resendNote.set(
          res?.email_sent === false
            ? 'Still could not send. Please contact your administrator.'
            : 'A new code has been sent. Check your inbox (and spam).',
        );
      },
      error: () => {
        this.resending.set(false);
        this.resendNote.set('Could not resend right now. Try again shortly.');
      },
    });
  }
}
