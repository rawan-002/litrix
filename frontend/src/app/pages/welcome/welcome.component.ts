// Landing page for unauthenticated visitors: quiet hero, one CTA, real numbers
// from the public-stats endpoint, and a feature grid. Behind guestGuard, so
// signed-in users get bounced to the dashboard.
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

  // Values that count up to the real numbers on mount.
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

  // Tween each stat from 0 to its target over ~800ms on one rAF loop.
  private animateCountUp(target: PublicStats) {
    const duration = 800;
    const start = performance.now();

    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      // ease-out cubic so it slows near the end
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

  // Smooth-scroll to an in-page section. preventDefault stops the router from
  // intercepting a bare href="#about" and forcing a reload.
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
