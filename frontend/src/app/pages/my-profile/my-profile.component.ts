/**
 * My Profile page — publication-only view.
 *
 * All data-update concerns (personal info, academic IDs, password) live
 * in SettingsComponent. This page is intentionally read-only so the
 * researcher lands directly on their publications.
 */
import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AuthService } from '../../core/services/auth.service';
import { ResearcherProfileComponent } from
  '../../components/researcher-profile/researcher-profile.component';


@Component({
  selector: 'app-my-profile',
  standalone: true,
  imports: [CommonModule, ResearcherProfileComponent],
  templateUrl: './my-profile.component.html',
})
export class MyProfileComponent {
  protected auth = inject(AuthService);
}
