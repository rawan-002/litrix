/**
 * Root App Shell - fixed top bar with logo + search.
 *
 * Layout uses solid bg-white (not transparent) to ensure visibility
 * over the body's ink-100 background. Border-b adds a clear separation.
 */
import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';

/**
 * Legacy AppComponent - kept for compatibility with any code still
 * importing it. The active bootstrap target is `App` in app.ts.
 * The right Researchers sidebar was removed; LayoutComponent now owns
 * the entire chrome.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet],
  template: `<router-outlet />`,
})
export class AppComponent {}
