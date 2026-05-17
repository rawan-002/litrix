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
  // Severity drives the banner color: high = red (block-worthy issue),
  // info = blue (informational), default amber for moderate mismatches.
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

  // Identity-verification state — populated by the debounced check
  // against /api/auth/registration-match/ as the form is filled in.
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

  /**
   * Why split first/last name on the form when the backend stores
   * `FullName_Ar` and `FirstName/LastName` directly?
   *
   *   • English side: the User table already has separate FirstName /
   *     LastName columns; sending them split avoids the lossy
   *     `full_name.split(' ')[0]` heuristic in approve_registration.
   *   • Arabic side: the schema currently keeps a single FullName_Ar
   *     column, so we concatenate first + last on submit. If we ever
   *     add FirstName_Ar / LastName_Ar columns later, no UI rework is
   *     needed — the form is already split.
   */
  readonly profileForm = this.fb.nonNullable.group({
    email:           ['', [Validators.required, Validators.email]],
    password:        ['', [Validators.required, Validators.minLength(8)]],
    // English name is REQUIRED — it's the canonical form used in
    // citation lookups, journal records, and admin reporting.
    first_name_en:   ['', [Validators.required]],
    last_name_en:    ['', [Validators.required]],
    // Arabic name is OPTIONAL — preserved for the local UI and
    // FullName_Ar column, but a missing value is acceptable. Researchers
    // imported via scrapers may not always have an Arabic name yet.
    first_name_ar:   [''],
    last_name_ar:    [''],
    department_id:   [null as number | null],
    academic_rank:   [''],
    scholar_id:      [''],
    orcid_id:        [''],
  });

  /** Concatenated full names — what the backend currently expects. */
  private buildFullNames() {
    const v = this.profileForm.getRawValue();
    const fullAr = [v.first_name_ar, v.last_name_ar]
      .map(s => (s || '').trim()).filter(Boolean).join(' ');
    const fullEn = [v.first_name_en, v.last_name_en]
      .map(s => (s || '').trim()).filter(Boolean).join(' ');
    return { full_name_ar: fullAr, full_name_en: fullEn };
  }

  readonly verifyForm = this.fb.nonNullable.group({
    token: ['', [Validators.required, Validators.minLength(6)]],
  });

  readonly registeredEmail = signal<string>('');

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
    // Pick up an invite token from the URL early so the form can lock
    // its email/department fields before any user input fires.
    const t = this.route.snapshot.queryParamMap.get('invite');
    if (t) {
      this.inviteToken.set(t);
      this.http.get<InvitePayload>(
        `${environment.apiBaseUrl}/auth/invitations/lookup/${encodeURIComponent(t)}/`,
      ).subscribe({
        next: payload => {
          this.invite.set(payload);
          if (payload.valid) {
            // Pre-fill + freeze the fields the invitation already pinned.
            const patch: any = {};
            if (payload.invited_email) patch.email = payload.invited_email;
            if (payload.department_id) patch.department_id = payload.department_id;
            this.profileForm.patchValue(patch);
            // Email is bound to the invitation; locking it prevents
            // accidental mismatch errors at submit time.
            this.profileForm.get('email')?.disable();
            if (payload.department_id) {
              this.profileForm.get('department_id')?.disable();
            }
          }
        },
        error: () => this.invite.set({ valid: false, reason: 'lookup_failed' }),
      });
    }

    // Wire the debounced verification pipeline. We re-check whenever a
    // relevant field changes so the warning is always in sync with the
    // form state. Trip-wire the Subject from the form's valueChanges.
    this.matchPing$
      .pipe(
        debounceTime(400),
        switchMap(() => {
          const v = this.profileForm.getRawValue();
          // Need at least one identity key, otherwise nothing to match.
          if (!v.scholar_id && !v.orcid_id && !v.email) {
            return of(null);
          }
          this.verifying.set(true);
          const names = this.buildFullNames();
          return this.http.post<VerificationResult>(
            `${environment.apiBaseUrl}/auth/registration-match/`,
            {
              scholar_id:    v.scholar_id || null,
              orcid_id:      v.orcid_id || null,
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

    // Fire on any change to identity-relevant fields.
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
    const payload: any = {
      email:         v.email,
      password:      v.password,
      full_name_ar:  names.full_name_ar,
      full_name_en:  names.full_name_en,
      first_name:    (v.first_name_en || '').trim(),
      last_name:     (v.last_name_en  || '').trim(),
      department_id: v.department_id,
      academic_rank: v.academic_rank,
      scholar_id:    v.scholar_id,
      orcid_id:      v.orcid_id,
    };
    // Carry the invite token through if we have one — the backend uses
    // it to skip the awaiting-email-verification queue entirely.
    if (this.inviteToken()) {
      payload.invite = this.inviteToken();
    }
    this.auth.register(payload).subscribe({
      next: (res: any) => {
        this.loading.set(false);
        // Invitation flow returns a created user_id and skips email
        // verification. Bounce straight to the login page with success.
        if (this.inviteToken() && res?.user_id) {
          this.step.set('done');
          return;
        }
        this.registeredEmail.set(payload.email);
        this.step.set('verify');
      },
      error: err => {
        this.loading.set(false);
        // 409 from the identity-verification gate — surface the same
        // banner the real-time check uses so the user sees exactly
        // what's wrong and stays on the profile step.
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
}
