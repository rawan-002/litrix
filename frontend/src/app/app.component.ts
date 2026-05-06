/**
 * Root App Shell — layout with right sidebar.
 *
 * Why a sidebar (vs. a search modal):
 *   - Search-while-browsing: user can search a researcher without
 *     leaving the current page (overview, profile, etc.)
 *   - Apple-style spatial consistency: search lives in a fixed corner,
 *     reducing cognitive load
 *   - On RTL Arabic, the right sidebar is the natural primary slot
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
    <div class="min-h-screen flex flex-row-reverse" dir="rtl">
      <!-- Right sidebar (search + future tools) -->
      <aside class="w-80 shrink-0 border-l border-stone-200 bg-stone-50/60
                    sticky top-0 h-screen overflow-y-auto">
        <app-researcher-search-sidebar />
      </aside>

      <!-- Main scrollable area -->
      <main class="flex-1 min-w-0">
        <router-outlet />
      </main>
    </div>
  `,
})
export class AppComponent {}
