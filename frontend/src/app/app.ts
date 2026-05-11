/**
 * Root App Shell — minimal pass-through.
 *
 * The full layout (sidebar nav, header, content) lives in
 * `shared/layout/layout.component.ts` and is mounted as a parent route.
 * Keeping this shell empty means each route owns its own chrome and
 * there's no duplicated layout to fight against.
 */
import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {}
