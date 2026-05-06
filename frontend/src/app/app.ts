/**
 * Root App Shell — collapsible right sidebar.
 *
 * The sidebar can be toggled open/closed via a small handle button.
 * State persists in localStorage so it stays open/closed across reloads.
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
  // Persist sidebar state across reloads.
  protected readonly sidebarOpen = signal<boolean>(
    typeof localStorage !== 'undefined'
      ? localStorage.getItem('litrix.sidebarOpen') !== 'false'
      : true
  );

  toggleSidebar() {
    const next = !this.sidebarOpen();
    this.sidebarOpen.set(next);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('litrix.sidebarOpen', String(next));
    }
  }
}
