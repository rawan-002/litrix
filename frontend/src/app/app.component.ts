/**
 * Root App Shell — layout with right sidebar.
 *
 * Layout strategy (RTL Arabic):
 *   - dir="rtl" on the wrapper → text flows right-to-left
 *   - flex (default flex-row) in RTL → first child is on the RIGHT
 *   - So <aside> first → sidebar on right ✓
 *   - <main> second → main content on left ✓
 *
 * No `flex-row-reverse` needed; that would actually flip the order
 * back (RTL + reverse = LTR effectively).
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
    <div class="min-h-screen flex" dir="rtl">
      <!-- Right sidebar (search + future tools).
           Uses ink-* (project palette) for consistency with the rest
           of the dashboard. Border-l + white bg makes it pop against
           the body's ink-100 backdrop. -->
      <aside class="w-80 shrink-0 border-l border-ink-200 bg-white
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
