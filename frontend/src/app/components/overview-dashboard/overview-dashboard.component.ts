/**
 * Overview Dashboard — Apple-style landing page.
 * Year toggle filters all KPI cards. Export button opens a modal with
 * fine-grained options (years + sheets) before triggering the download.
 */
import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { LitrixApiService } from '../../services/litrix-api.service';
import {
  OverviewPayload, YearlyBreakdownPayload, PaperDetail,
} from '../../models/litrix.models';
import { environment } from '../../../environments/environment';
import { PaperDetailModalComponent } from
  '../paper-detail-modal/paper-detail-modal.component';

type ScopeYear = 'all' | 2025 | 2026;

@Component({
  selector: 'app-overview-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, PaperDetailModalComponent],
  templateUrl: './overview-dashboard.component.html',
})
export class OverviewDashboardComponent implements OnInit {
  private readonly api = inject(LitrixApiService);

  readonly data    = signal<OverviewPayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);
  readonly scope   = signal<ScopeYear>('all');

  readonly availableYears = [2026, 2025];
  readonly selectedYear   = signal<number>(2026);
  readonly yearlyData     = signal<YearlyBreakdownPayload | null>(null);
  readonly yearlyLoading  = signal<boolean>(false);
  readonly expandedDeptId = signal<number | null>(null);

  readonly showExportModal = signal<boolean>(false);
  exportYears = { 2025: true, 2026: true };
  exportSheets = {
    summary:     true,
    departments: true,
    researchers: true,
    journals:    true,
    conferences: true,
  };

  ngOnInit() {
    this.loadOverview();
    this.loadYear(this.selectedYear());
  }

  loadOverview() {
    this.loading.set(true);
    const year = this.scope() === 'all' ? undefined : Number(this.scope());
    this.api.getOverview(year).subscribe({
      next: (payload) => {
        this.data.set(payload);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`Failed to load: ${err.message}`);
        this.loading.set(false);
      },
    });
  }

  setScope(s: ScopeYear) {
    this.scope.set(s);
    this.loadOverview();
  }

  loadYear(year: number) {
    this.selectedYear.set(year);
    this.expandedDeptId.set(null);
    this.yearlyLoading.set(true);
    this.api.getYearlyBreakdown(year).subscribe({
      next: (payload) => {
        this.yearlyData.set(payload);
        this.yearlyLoading.set(false);
      },
      error: () => this.yearlyLoading.set(false),
    });
  }

  toggleDept(deptId: number) {
    this.expandedDeptId.set(
      this.expandedDeptId() === deptId ? null : deptId
    );
  }

  // Paper detail modal — store just the paper_id; the modal component
  // fetches the full details on demand.
  readonly selectedPaperId = signal<number | null>(null);

  openPaper(p: { paper_id: number }) { this.selectedPaperId.set(p.paper_id); }
  closePaper()                       { this.selectedPaperId.set(null); }

  papersFor(deptId: number, venueType: 'Journal' | 'Conference'): PaperDetail[] {
    const papers = this.yearlyData()?.papers ?? [];
    return papers.filter(p =>
      p.department_id === deptId && p.venue_type === venueType
    );
  }

  readonly LOAD_BATCH = 10;
  visibleCounts: Record<string, number> = {};

  visibleCount(deptId: number, venueType: 'Journal' | 'Conference'): number {
    return this.visibleCounts[`${deptId}-${venueType}`] || this.LOAD_BATCH;
  }

  loadMore(deptId: number, venueType: 'Journal' | 'Conference'): void {
    const key = `${deptId}-${venueType}`;
    this.visibleCounts[key] = this.visibleCount(deptId, venueType) + this.LOAD_BATCH;
  }

  papersForLimited(deptId: number, venueType: 'Journal' | 'Conference'): PaperDetail[] {
    return this.papersFor(deptId, venueType)
      .slice(0, this.visibleCount(deptId, venueType));
  }

  hasMore(deptId: number, venueType: 'Journal' | 'Conference'): boolean {
    return this.papersFor(deptId, venueType).length > this.visibleCount(deptId, venueType);
  }

  fmt(n: number | null | undefined): string {
    if (n == null) return '—';
    return n.toLocaleString('en-US');
  }

  /** Sum across by_year breakdown — used for the "Total" row in the
   *  per-department mini-table. Avoids storing the total separately. */
  deptTotalPapers(dept: any): number {
    return (dept.by_year || []).reduce(
      (acc: number, y: any) => acc + (y.papers || 0), 0,
    );
  }

  deptTotalCitations(dept: any): number {
    return (dept.by_year || []).reduce(
      (acc: number, y: any) => acc + (y.citations || 0), 0,
    );
  }

  openExportModal()  { this.showExportModal.set(true); }
  closeExportModal() { this.showExportModal.set(false); }

  triggerExport() {
    const years = Object.entries(this.exportYears)
      .filter(([_, v]) => v)
      .map(([y]) => y)
      .join(',');
    const sheets = Object.entries(this.exportSheets)
      .filter(([_, v]) => v)
      .map(([s]) => s)
      .join(',');

    if (!years) {
      alert('Please select at least one year');
      return;
    }
    if (!sheets) {
      alert('Please select at least one sheet');
      return;
    }

    const url = `${environment.apiBaseUrl}/export/excel/?years=${years}&sheets=${sheets}`;
    window.location.href = url;
    this.closeExportModal();
  }
}
