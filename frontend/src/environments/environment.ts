/**
 * Development environment.
 * Used by `ng serve` (the dev server).
 *
 * Why a separate file? So we never accidentally ship localhost URLs to
 * production. The build replaces this with environment.prod.ts via the
 * fileReplacements rule in angular.json.
 */
export const environment = {
  production: false,
  apiBaseUrl: 'http://localhost:8000/api',
};
