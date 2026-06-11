import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService } from '../services/auth.service';


export const authGuard: CanActivateFn = (route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  if (auth.isAuthenticated()) {
    return true;
  }
  // Signed-out visitors get /welcome first; returnUrl is kept so login
  // can send them back to the route they were after.
  return router.createUrlTree(['/welcome'], {
    queryParams: { returnUrl: state.url },
  });
};


export const permissionGuard = (...required: string[]): CanActivateFn => {
  return () => {
    const auth = inject(AuthService);
    const router = inject(Router);

    if (!auth.isAuthenticated()) {
      // Same /welcome redirect as authGuard + the jwt interceptor.
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
