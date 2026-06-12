import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../../../environments/environment';


interface PendingRequest {
  RequestID: number;
  Email: string;
  FullName_Ar: string | null;
  FullName_En: string | null;
  Scholar_ID: string | null;
  Orcid_ID: string | null;
  Scopus_ID: string | null;
  DepartmentID: number | null;
  // Resolved server-side via LEFT JOIN, so no separate /departments lookup.
  departmentname: string | null;
  AcademicRank: string | null;
  Status: string;
  CreatedAt: string;
}


@Component({
  selector: 'app-registrations',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './registrations.component.html',
})
export class RegistrationsComponent {
  private http = inject(HttpClient);
  private API = `${environment.apiBaseUrl}/auth`;

  readonly requests = signal<PendingRequest[]>([]);
  readonly loading = signal(true);
  readonly processing = signal<number | null>(null);
  readonly rejectionReason = signal<{ [id: number]: string }>({});
  readonly showRejectFor = signal<number | null>(null);

  constructor() {
    this.load();
  }

  load() {
    this.loading.set(true);
    this.http.get<{ requests: PendingRequest[] }>(`${this.API}/registrations/`).subscribe({
      next: res => {
        this.requests.set(res.requests || []);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  approve(id: number) {
    this.processing.set(id);
    this.http.post<{
      message: string;
      user_id: number;
      sync_kicked: boolean;
      sync_source: 'scholar' | 'orcid' | null;
    }>(`${this.API}/registrations/${id}/approve/`, {}).subscribe({
      next: res => {
        this.requests.update(r => r.filter(x => x.RequestID !== id));
        this.processing.set(null);
        // Tell the admin the backend already queued the publication scrape, so
        // they don't go fire it by hand from the Sync page.
        if (res.sync_kicked) {
          alert(
            `Approved. Initial ${res.sync_source} sync queued - ` +
            `track progress in Sync Control.`,
          );
        }
      },
      error: () => this.processing.set(null),
    });
  }

  reject(id: number) {
    const reason = this.rejectionReason()[id] || '';
    this.processing.set(id);
    this.http.post(`${this.API}/registrations/${id}/reject/`, { reason }).subscribe({
      next: () => {
        this.requests.update(r => r.filter(x => x.RequestID !== id));
        this.processing.set(null);
        this.showRejectFor.set(null);
      },
      error: () => this.processing.set(null),
    });
  }

  toggleReject(id: number) {
    this.showRejectFor.update(v => v === id ? null : id);
  }

  setReason(id: number, value: string) {
    this.rejectionReason.update(r => ({ ...r, [id]: value }));
  }

  getReason(id: number): string {
    return this.rejectionReason()[id] || '';
  }

  formatDate(s: string): string {
    return new Date(s).toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  }
}
