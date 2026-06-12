/**
 * Author Reconciliation Component
 * ================================
 * Admin-facing page that surfaces author-paper matches whose confidence
 * fell below the auto-link threshold (< 0.70). The admin confirms, rejects,
 * or skips each suggestion.
 *
 * UX PRINCIPLES (per project guidelines):
 *  - One decision at a time. No bulk-action noise.
 *  - Generous white space. Minimal chrome.
 *  - Confidence shown as a soft horizontal bar, not a raw number.
 *  - Keyboard shortcuts: [C]onfirm, [R]eject, [S]kip - power-user friendly.
 *
 * PERMISSIONS:
 *  - This route is guarded by the AdminGuard. HoD/Dean cannot reach it.
 */

import { Component, OnInit, signal, computed, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

interface ReviewItem {
  reviewId: number;
  paperId: number;
  paperTitle: string;
  scrapedName: string;
  scrapedAffiliation: string | null;
  suggestedUserId: number | null;
  suggestedName: string | null;
  suggestedDepartment: string | null;
  suggestedConfidence: number;
  suggestedCriteria: string;
}

type Decision = 'CONFIRMED' | 'REJECTED' | 'SKIPPED';

@Component({
  selector: 'app-author-reconciliation',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './author-reconciliation.component.html',
  styleUrls: ['./author-reconciliation.component.scss'],
})
export class AuthorReconciliationComponent implements OnInit {
  private http = inject(HttpClient);

  // ---------- State ----------
  readonly queue       = signal<ReviewItem[]>([]);
  readonly cursor      = signal<number>(0);
  readonly isLoading   = signal<boolean>(false);
  readonly isSubmitting = signal<boolean>(false);

  readonly current = computed<ReviewItem | null>(() => {
    const list = this.queue();
    const idx  = this.cursor();
    return list[idx] ?? null;
  });

  readonly remaining = computed(() => this.queue().length - this.cursor());

  // ---------- Lifecycle ----------
  async ngOnInit(): Promise<void> {
    await this.loadQueue();
    this.bindKeyboardShortcuts();
  }

  // ---------- Data ----------
  private async loadQueue(): Promise<void> {
    this.isLoading.set(true);
    try {
      const data = await firstValueFrom(
        this.http.get<ReviewItem[]>('/api/admin/author-review-queue/?status=PENDING')
      );
      this.queue.set(data);
      this.cursor.set(0);
    } finally {
      this.isLoading.set(false);
    }
  }

  // ---------- Actions ----------
  async confirm(): Promise<void> { await this.decide('CONFIRMED'); }
  async reject():  Promise<void> { await this.decide('REJECTED'); }
  async skip():    Promise<void> { await this.decide('SKIPPED'); }

  private async decide(decision: Decision): Promise<void> {
    const item = this.current();
    if (!item || this.isSubmitting()) return;

    this.isSubmitting.set(true);
    try {
      await firstValueFrom(
        this.http.post(`/api/admin/author-review-queue/${item.reviewId}/decide/`, { decision })
      );
      this.advance();
    } finally {
      this.isSubmitting.set(false);
    }
  }

  private advance(): void {
    this.cursor.update(c => c + 1);
  }

  // ---------- UX helpers ----------
  confidencePct(item: ReviewItem): number {
    return Math.round(item.suggestedConfidence * 100);
  }

  confidenceTone(item: ReviewItem): 'high' | 'medium' | 'low' {
    const c = item.suggestedConfidence;
    if (c >= 0.85) return 'high';
    if (c >= 0.65) return 'medium';
    return 'low';
  }

  private bindKeyboardShortcuts(): void {
    document.addEventListener('keydown', (e) => {
      if (e.target instanceof HTMLInputElement) return;
      const key = e.key.toLowerCase();
      if (key === 'c') this.confirm();
      else if (key === 'r') this.reject();
      else if (key === 's') this.skip();
    });
  }
}
