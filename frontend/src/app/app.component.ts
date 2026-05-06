/**
 * Root App Shell — fixed top bar (header).
 *
 * Design rationale:
 *   - Top bar is always visible and doesn't compete with main content
 *     for horizontal space (good for tables, charts, dashboards).
 *   - Apple-style: thin, minimal, with backdrop blur.
 */
import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { ResearcherSearchSidebarComponent } from
  './components/researcher-search-sidebar/researcher-search-sidebar.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, ResearcherSearchSidebarComponent],
  template: `
    <div dir="rtl" class="min-h-screen">
      <!-- Fixed top bar with logo + search -->
      <header class="fixed top-0 inset-x-0 h-16 z-20
                     bg-white/80 backdrop-blur-md
                     border-b border-ink-200
                     flex items-center px-6 gap-6">
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
