import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, switchMap, throwError, of } from 'rxjs';
import { AuthService } from '../services/auth.service';


export const jwtInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  if (req.url.includes('/auth/login') ||
      req.url.includes('/auth/register') ||
      req.url.includes('/auth/verify-email') ||
      req.url.includes('/auth/password-reset') ||
      req.url.includes('/auth/refresh')) {
    return next(req);
  }

  const token = auth.getAccessToken();
  const authReq = token
    ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } })
    : req;

  return next(authReq).pipe(
    catchError((err: HttpErrorResponse) => {
      if (err.status === 401 && token) {
        return auth.refreshToken().pipe(
          switchMap(res => {
            if (!res.access) {
              // Token completely dead — send to the public landing
              // page so users don't get dropped onto a bare login form.
              router.navigate(['/welcome']);
              return throwError(() => err);
            }
            const retry = req.clone({
              setHeaders: { Authorization: `Bearer ${res.access}` },
            });
            return next(retry);
          }),
          catchError(() => {
            router.navigate(['/welcome']);
            return throwError(() => err);
          }),
        );
      }
      return throwError(() => err);
    }),
  );
};
