import { Injectable, signal, effect } from '@angular/core';

// Platform-wide "Al-Baha output only" switch. One header toggle drives every
// dashboard: when on, requests carry ?affiliation=albaha and the pages re-fetch.
// Persisted so the choice survives navigation and reload. Defaults to ON — the
// institution's own output is the primary view.
const STORAGE_KEY = 'litrix.albahaOnly';

@Injectable({ providedIn: 'root' })
export class AffiliationService {
  readonly albahaOnly = signal<boolean>(this.readInitial());

  constructor() {
    effect(() => {
      try {
        localStorage.setItem(STORAGE_KEY, this.albahaOnly() ? '1' : '0');
      } catch { /* storage unavailable (private mode) — ignore */ }
    });
  }

  toggle() {
    this.albahaOnly.update(v => !v);
  }

  /** The query-param value, or null when the filter is off. */
  param(): 'albaha' | null {
    return this.albahaOnly() ? 'albaha' : null;
  }

  private readInitial(): boolean {
    try {
      const v = localStorage.getItem(STORAGE_KEY);
      if (v === '0') return false;
      if (v === '1') return true;
    } catch { /* ignore */ }
    return true;  // default ON
  }
}
