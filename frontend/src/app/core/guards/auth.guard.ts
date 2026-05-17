import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService } from '../services/auth.service';


export const authGuard: CanActivateFn = (route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  if (auth.isAuthenticated()) {
    return true;
  }
  // Unauthenticated visitors land on /welcome (the public landing page)
  // so they see what the product is before being prompted to sign in.
  // returnUrl is preserved so the post-login flow can resume the
  // originally requested route.
  return router.createUrlTree(['/welcome'], {
    queryParams: { returnUrl: state.url },
  });
};


export const permissionGuard = (...required: string[]): CanActivateFn => {
  return () => {
    const auth = inject(AuthService);
    const router = inject(Router);

    if (!auth.isAuthenticated()) {
      // Unauthenticated visitors get the public landing page, not the
      // bare /login form — consistent with authGuard + jwt interceptor.
      return router.createUrlTree(['/welcome']);
    }
    if (!auth.hasAnyPermission(...required)) {
      return router.createUrlTree(['/forbidden']);
    }
    return true;
  };
};


export const guestGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth.isAuthenticated() ? router.createUrlTree(['/']) : true;
};
