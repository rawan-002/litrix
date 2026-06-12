import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Router } from '@angular/router';
import { Observable, tap, of, catchError } from 'rxjs';
import { environment } from '../../../environments/environment';


export interface AuthUser {
  user_id: number;
  // Public Lit-NNNNNN id used in URLs, sharing and disambiguation.
  // Kept separate from user_id so URLs survive DB migrations/re-imports.
  litrix_id: string;
  email: string;
  full_name: string;
  full_name_ar: string | null;
  user_type: string;
  tenant_id: number;
  role_id: number | null;
  role: string | null;
  permissions: string[];
  email_verified: boolean;
  scholar_id: string | null;
  orcid_id: string | null;
  scopus_id: string | null;
  photo_url: string | null;
}

interface LoginResponse {
  access: string;
  refresh: string;
  user: AuthUser;
}


@Injectable({ providedIn: 'root' })
export class AuthService {
  private http = inject(HttpClient);
  private router = inject(Router);

  private API = `${environment.apiBaseUrl}/auth`;

  readonly user = signal<AuthUser | null>(this.loadUser());
  readonly isAuthenticated = computed(() => !!this.user() && !!this.getAccessToken());
  readonly role = computed(() => this.user()?.role ?? null);
  readonly permissions = computed(() => new Set(this.user()?.permissions ?? []));

  constructor() {}

  hasPermission(code: string): boolean {
    return this.permissions().has(code);
  }

  hasAnyPermission(...codes: string[]): boolean {
    return codes.some(c => this.permissions().has(c));
  }

  login(email: string, password: string): Observable<LoginResponse> {
    return this.http.post<LoginResponse>(`${this.API}/login/`, { email, password }).pipe(
      tap(res => {
        this.persistSession(res);
        // Clear once-per-session flags so a fresh login re-fires them
        // (e.g. the welcome fireworks on the dashboard).
        try { sessionStorage.removeItem('litrix_welcome_v1'); } catch { /* ignore */ }
      }),
    );
  }

  register(payload: any): Observable<{ message: string; request_id: number; email_sent?: boolean; user_id?: number }> {
    return this.http.post<{ message: string; request_id: number; email_sent?: boolean; user_id?: number }>(
      `${this.API}/register/`, payload,
    );
  }

  verifyEmail(email: string, token: string): Observable<any> {
    return this.http.post(`${this.API}/verify-email/`, { email, token });
  }

  /** Re-send the registration verification code to an unverified email. */
  resendVerification(email: string): Observable<{ message: string; email_sent: boolean }> {
    return this.http.post<{ message: string; email_sent: boolean }>(
      `${this.API}/resend-verification/`, { email },
    );
  }

  passwordResetRequest(email: string): Observable<any> {
    return this.http.post(`${this.API}/password-reset/`, { email });
  }

  passwordResetConfirm(email: string, token: string, new_password: string): Observable<any> {
    return this.http.post(`${this.API}/password-reset/confirm/`, {
      email, token, new_password,
    });
  }

  refreshToken(): Observable<{ access: string; refresh?: string }> {
    const refresh = this.getRefreshToken();
    if (!refresh) {
      this.logout();
      return of({ access: '' });
    }
    return this.http.post<{ access: string; refresh?: string }>(
      `${this.API}/refresh/`, { refresh },
    ).pipe(
      tap(res => {
        localStorage.setItem('litrix_access', res.access);
        if (res.refresh) {
          localStorage.setItem('litrix_refresh', res.refresh);
        }
      }),
      catchError(err => {
        this.logout();
        throw err;
      }),
    );
  }

  fetchMe(): Observable<AuthUser> {
    return this.http.get<AuthUser>(`${this.API}/me/`).pipe(
      tap(u => {
        this.user.set(u);
        localStorage.setItem('litrix_user', JSON.stringify(u));
      }),
    );
  }

  logout(): void {
    const refresh = this.getRefreshToken();
    if (refresh) {
      this.http.post(`${this.API}/logout/`, { refresh }).subscribe({
        error: () => {},
      });
    }
    this.clearSession();
    // Land on /welcome rather than a bare /login form — "Sign in" is
    // still right there in the nav for anyone who wants it.
    this.router.navigate(['/welcome']);
  }

  getAccessToken(): string | null {
    return localStorage.getItem('litrix_access');
  }

  getRefreshToken(): string | null {
    return localStorage.getItem('litrix_refresh');
  }

  private persistSession(res: LoginResponse): void {
    localStorage.setItem('litrix_access', res.access);
    localStorage.setItem('litrix_refresh', res.refresh);
    localStorage.setItem('litrix_user', JSON.stringify(res.user));
    this.user.set(res.user);
  }

  private clearSession(): void {
    localStorage.removeItem('litrix_access');
    localStorage.removeItem('litrix_refresh');
    localStorage.removeItem('litrix_user');
    this.user.set(null);
  }

  private loadUser(): AuthUser | null {
    const raw = localStorage.getItem('litrix_user');
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }
}
