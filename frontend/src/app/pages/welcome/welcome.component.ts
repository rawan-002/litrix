/**
 * Welcome (Landing) page - shown to unauthenticated visitors.
 *
 * DESIGN
 * ------
 * Apple-style minimalist: large quiet hero, single primary CTA, real
 * stats pulled from the public-stats endpoint, then a feature grid.
 * No carousels, no animations beyond a soft count-up on the stats so
 * the page feels alive without screaming.
 *
 * ROUTING
 * -------
 * Guarded by guestGuard - already-authenticated users get bounced to
 * the dashboard. The "Sign in" button drops them on /login.
 */
import { Component, OnInit, signal, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

interface PublicStats {
  researchers:      number;
  papers:           number;
  q1_journals:      number;
  departments:      number;
  papers_this_year: number;
}

@Component({
  selector: 'app-welcome',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './welcome.component.html',
  styleUrls: ['./welcome.component.scss'],
})
export class WelcomeComponent implements OnInit {
  private http   = inject(HttpClient);
  private router = inject(Router);

  readonly stats   = signal<PublicStats | null>(null);
  readonly loading = signal<boolean>(true);
  readonly year    = new Date().getFullYear();

  /** Animated values that count up to the real numbers on mount. */
  readonly displayedStats = signal<PublicStats>({
    researchers: 0, papers: 0, q1_journals: 0, departments: 0,
    papers_this_year: 0,
  });

  async ngOnInit(): Promise<void> {
    try {
      const data = await firstValueFrom(
        this.http.get<PublicStats>('/api/accounts/public-stats/')
      );
      this.stats.set(data);
      this.animateCountUp(data);
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Tween each stat from 0 to its target over ~800 ms using a single
   * requestAnimationFrame loop - cheap, smooth, no dependencies.
   */
  private animateCountUp(target: PublicStats) {
    const duration = 800;
    const start = performance.now();

    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      // ease-out cubic so the count slows down near the end
      const k = 1 - Math.pow(1 - t, 3);
      this.displayedStats.set({
        researchers:      Math.round(target.researchers      * k),
        papers:           Math.round(target.papers           * k),
        q1_journals:      Math.round(target.q1_journals      * k),
        departments:      Math.round(target.departments      * k),
        papers_this_year: Math.round(target.papers_this_year * k),
      });
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  fmt(n: number): string {
    return n.toLocaleString('en-US');
  }

  /**
   * Smooth scroll to an in-page section without triggering an
   * Angular route change. Prevents the browser from doing a full
   * reload (which is what happens with bare href="#about" inside
   * an Angular app because the router intercepts the URL update).
   */
  scrollTo(sectionId: string, event: Event): void {
    event.preventDefault();
    const target = document.getElementById(sectionId);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  goToLogin()    { this.router.navigate(['/login']); }
  goToRegister() { this.router.navigate(['/register']); }
}
