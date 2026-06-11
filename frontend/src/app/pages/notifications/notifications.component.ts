import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { NotificationsService } from '../../core/services/notifications.service';


@Component({
  selector: 'app-notifications',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './notifications.component.html',
})
export class NotificationsComponent {
  protected svc = inject(NotificationsService);

  readonly loading = signal(true);
  readonly filter = signal<'all' | 'unread'>('all');

  constructor() {
    this.load();
  }

  load() {
    this.loading.set(true);
    this.svc.load(this.filter() === 'unread').subscribe({
      next: () => this.loading.set(false),
      error: () => this.loading.set(false),
    });
  }

  setFilter(f: 'all' | 'unread') {
    this.filter.set(f);
    this.load();
  }

  markRead(id: number) {
    this.svc.markRead(id).subscribe();
  }

  markAllRead() {
    this.svc.markAllRead().subscribe();
  }

  formatDate(s: string): string {
    const d = new Date(s);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return `${diffDay}d ago`;
    return d.toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  }

  iconFor(type: string): string {
    if (type.startsWith('registration')) return '✓';
    if (type.startsWith('sync')) return '↻';
    if (type.startsWith('paper')) return '▤';
    return '⚑';
  }
}
