/**
 * Root App Shell — fixed top bar with logo + search.
 *
 * Note: this is the ACTUAL root component (bootstrapped by main.ts).
 * The class name is `App` (not `AppComponent`) for compatibility with
 * the existing Angular CLI scaffold.
 */
import { Component, signal } from '@angular/core';
import { RouterOutlet, RouterLink } from '@angular/router';
import { ResearcherSearchSidebarComponent } from
  './components/researcher-search-sidebar/researcher-search-sidebar.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, ResearcherSearchSidebarComponent],
  templateUrl: './app.html',
  styleUrl: './app.css'
})
export class App {
  protected readonly title = signal('frontend');
}
