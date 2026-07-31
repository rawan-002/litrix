import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface LogEntry {
  LogID: number;
  Action: string;
  TargetType: string | null;
  TargetID: number | null;
  Metadata: any;
  IpAddress: string | null;
  CreatedAt: string;
  Email: string | null;
  FullName_Ar: string | null;
  FullName_En: string | null;
}


@Component({
  selector: 'app-audit',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './audit.component.html',
})
export class AuditComponent {
  private http = inject(HttpClient);
  readonly logs = signal<LogEntry[]>([]);
  readonly loading = signal(true);
  readonly actionFilter = signal('');

  constructor() { this.load(); }

  load() {
    this.loading.set(true);
    const q = this.actionFilter() ? `?action=${this.actionFilter()}` : '';
    this.http.get<{ logs: LogEntry[] }>(
      `${environment.apiBaseUrl}/auth/audit-log/${q}`,
    ).subscribe({
      next: r => { this.logs.set(r.logs || []); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
  }

  formatDate(s: string): string {
    return new Date(s).toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }

  formatMeta(meta: any): string {
    if (!meta || Object.keys(meta).length === 0) return '';
    return JSON.stringify(meta);
  }

  badgeClass(action: string): string {
    if (action.startsWith('auth.login')) return 'bg-blue-50 text-blue-700';
    if (action.startsWith('auth.')) return 'bg-purple-50 text-purple-700';
    if (action.startsWith('registration.approve')) return 'bg-green-50 text-green-700';
    if (action.startsWith('registration.reject')) return 'bg-red-50 text-red-700';
    if (action.startsWith('user.')) return 'bg-amber-50 text-amber-700';
    return 'bg-ink-100 text-ink-700';
  }
}
