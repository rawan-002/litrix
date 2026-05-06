/**
 * Paper Detail Modal — full paper info as in Google Scholar:
 *   • Title, raw authors, abstract, year, publisher
 *   • Total citations, citations-over-time chart
 *   • Journal/Conference info, DOI, link
 *
 * Used by both overview-dashboard (Top Papers + Yearly Breakdown) and
 * researcher-profile (per-researcher papers list). Reusable.
 */
import { Component, Input, Output, EventEmitter, inject, signal, computed, OnChanges, SimpleChanges } from '@angular/core';
import { CommonModule } from '@angular/common';
import { LitrixApiService } from '../../services/litrix-api.service';

@Component({
  selector: 'app-paper-detail-modal',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './paper-detail-modal.component.html',
})
export class PaperDetailModalComponent implements OnChanges {
  private readonly api = inject(LitrixApiService);

  @Input() paperId: number | null = null;
  @Output() closed = new EventEmitter<void>();

  readonly data    = signal<any | null>(null);
  readonly loading = signal<boolean>(false);
  readonly error   = signal<string | null>(null);

  // Mini bar chart for citations-over-time
  readonly chart = computed(() => {
    const cby = this.data()?.citations_by_year;
    if (!cby || typeof cby !== 'object') return null;
    const entries = Object.entries(cby)
      .map(([year, count]) => ({ year, count: Number(count) || 0 }))
      .sort((a, b) => a.year.localeCompare(b.year));
    if (entries.length === 0) return null;
    const max = Math.max(...entries.map(e => e.count), 1);
    const W = 460, H = 90, padding = { top: 8, right: 8, bottom: 22, left: 28 };
    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const step = innerW / entries.length;
    const barW = step * 0.65;
    return {
      W, H, padding,
      bars: entries.map((e, i) => {
        const h = (e.count / max) * innerH;
        return {
          year: e.year,
          count: e.count,
          x: padding.left + i * step + (step - barW) / 2,
          y: padding.top + innerH - h,
          w: barW,
          h,
        };
      }),
      max,
    };
  });

  ngOnChanges(changes: SimpleChanges): void {
    if ('paperId' in changes) {
      const id = changes['paperId'].currentValue;
      if (id) this.load(id);
      else this.data.set(null);
    }
  }

  load(id: number) {
    this.loading.set(true);
    this.error.set(null);
    this.data.set(null);
    this.api.getPaperDetail(id).subscribe({
      next: payload => {
        this.data.set(payload);
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err.status === 404
          ? 'Paper not found'
          : `Failed to load: ${err.message}`);
        this.loading.set(false);
      },
    });
  }

  close() {
    this.closed.emit();
  }

  fmt(n: number | null | undefined): string {
    if (n == null) return '—';
    return Number(n).toLocaleString('en-US');
  }

  encodeURIComponent(s: string | null | undefined): string {
    return s ? window.encodeURIComponent(s) : '';
  }
}
