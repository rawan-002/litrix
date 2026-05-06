/**
 * Root App Shell — fixed top bar with logo + search.
 *
 * Layout uses solid bg-white (not transparent) to ensure visibility
 * over the body's ink-100 background. Border-b adds a clear separation.
 */
import { Component } from '@angular/core';
import { RouterOutlet, RouterLink } from '@angular/router';
import { ResearcherSearchSidebarComponent } from
  './components/researcher-search-sidebar/researcher-search-sidebar.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, ResearcherSearchSidebarComponent],
  template: `
    <div dir="rtl" class="min-h-screen">
      <!-- Fixed top bar — solid white, visible border, persistent across routes -->
      <header class="fixed top-0 inset-x-0 h-16 z-20
                     bg-white border-b border-ink-200
                     flex items-center px-6 gap-4 shadow-card">

        <!-- Logo / brand -->
        <a routerLink="/" class="flex items-center gap-2 text-base font-semibold
                                  text-ink-900 hover:text-accent transition-colors
                                  whitespace-nowrap">
          <span class="text-2xl">📚</span>
          <span>Litrix</span>
        </a>

        <span class="text-ink-300 hidden md:inline">|</span>

        <span class="text-xs text-ink-400 hidden md:inline whitespace-nowrap">
          College of Computing & IT — Al-Baha University
        </span>

        <!-- Spacer -->
        <div class="flex-1"></div>

        <!-- Search component -->
        <app-researcher-search-sidebar />
      </header>

      <!-- Main content offset by header height -->
      <main class="pt-16 min-h-screen">
        <router-outlet />
      </main>
    </div>
  `,
})
export class AppComponent {}
