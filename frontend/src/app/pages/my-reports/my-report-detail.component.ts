// The verification surface: paper cards with Confirm / Not-mine toggles, a
// missing-papers section, and a submit that locks the report. The backend's
// submission.is_editable flag is the source of truth for whether anything is
// editable — we never second-guess it on the client.
import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { LitrixApiService } from '../../services/litrix-api.service';


interface PaperRow {
  paper_id: number;
  title: string;
  title_en: string | null;
  doi: string | null;
  pub_year: number;
  source: string | null;
  journal_name: string | null;
  quartile: string | null;
  citations: number;
  decision: {
    decision_id: number;
    decision: 'confirmed' | 'not_mine';
    note: string | null;
    decided_at: string;
  } | null;
}

interface MissingEntry {
  decision_id: number;
  title: string;
  doi: string | null;
  year: number;
  note: string | null;
  decided_at: string;
  resolved_at: string | null;
  resolved_to_paper_id: number | null;
}


@Component({
  selector: 'app-my-report-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './my-report-detail.component.html',
})
export class MyReportDetailComponent {
  private api = inject(LitrixApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  readonly submissionId = signal<number>(0);

  readonly campaign     = signal<any>(null);
  readonly submission   = signal<any>(null);
  readonly papers       = signal<PaperRow[]>([]);
  readonly missing      = signal<MissingEntry[]>([]);
  readonly loading      = signal(true);
  readonly busyPaperId  = signal<number | null>(null);
  readonly busySubmit   = signal(false);
  readonly error        = signal<string | null>(null);

  // Inline form for adding a missing paper.
  readonly showMissingForm = signal(false);
  readonly missingDraft    = signal({ title: '', year: 0, doi: '' });

  readonly summary = computed(() => {
    const ps = this.papers();
    return {
      total:      ps.length,
      confirmed:  ps.filter(p => p.decision?.decision === 'confirmed').length,
      not_mine:   ps.filter(p => p.decision?.decision === 'not_mine').length,
      undecided:  ps.filter(p => !p.decision).length,
      missing:    this.missing().length,
    };
  });

  constructor() {
    this.route.paramMap.subscribe(pm => {
      const id = Number(pm.get('id'));
      this.submissionId.set(id);
      this.refresh(id);
    });
  }

  refresh(id: number) {
    this.loading.set(true);
    this.api.getMySubmission(id).subscribe({
      next: r => {
        this.submission.set(r.submission);
        this.campaign.set(r.campaign);
        this.papers.set(r.papers || []);
        this.missing.set(r.missing || []);
        this.loading.set(false);
        // Pre-fill the form year with the first target year so the researcher
        // isn't starting from a blank field.
        if (r.campaign?.target_years?.[0]) {
          this.missingDraft.update(d => ({
            ...d, year: r.campaign.target_years[0],
          }));
        }
      },
      error: e => {
        this.error.set(this.errMsg(e));
        this.loading.set(false);
      },
    });
  }

  decide(paper: PaperRow, decision: 'confirmed' | 'not_mine') {
    if (!this.submission()?.is_editable) return;
    if (this.busyPaperId() === paper.paper_id) return;

    this.busyPaperId.set(paper.paper_id);
    this.api.recordDecision(this.submissionId(), {
      paper_id: paper.paper_id,
      decision,
    }).subscribe({
      next: r => {
        this.busyPaperId.set(null);
        // Patch the card in place instead of re-fetching — the response's
        // decision_id is enough to mark it decided.
        this.papers.update(list => list.map(p =>
          p.paper_id === paper.paper_id
            ? {
                ...p,
                decision: {
                  decision_id: r.decision_id,
                  decision:    decision,
                  note:        null,
                  decided_at:  new Date().toISOString(),
                },
              }
            : p
        ));
      },
      error: e => {
        this.busyPaperId.set(null);
        this.error.set(this.errMsg(e));
      },
    });
  }

  undecide(paper: PaperRow) {
    if (!this.submission()?.is_editable || !paper.decision) return;
    const decId = paper.decision.decision_id;
    this.busyPaperId.set(paper.paper_id);
    this.api.deleteDecision(this.submissionId(), decId).subscribe({
      next: () => {
        this.busyPaperId.set(null);
        this.papers.update(list => list.map(p =>
          p.paper_id === paper.paper_id ? { ...p, decision: null } : p
        ));
      },
      error: e => {
        this.busyPaperId.set(null);
        this.error.set(this.errMsg(e));
      },
    });
  }

  addMissing() {
    const m = this.missingDraft();
    if (!m.title.trim() || !m.year) {
      this.error.set('Title and year are required');
      return;
    }
    this.api.addMissingPaper(this.submissionId(), {
      title: m.title.trim(),
      year:  m.year,
      doi:   m.doi.trim() || undefined,
    }).subscribe({
      next: r => {
        // Prepend optimistically; a full reload happens on the next visit.
        this.missing.update(list => [{
          decision_id: r.decision_id,
          title:       m.title.trim(),
          doi:         m.doi.trim() || null,
          year:        m.year,
          note:        null,
          decided_at:  new Date().toISOString(),
          resolved_at: null,
          resolved_to_paper_id: null,
        }, ...list]);
        this.missingDraft.update(d => ({
          ...d, title: '', doi: '',  // keep the year so repeat adds are quick
        }));
        this.showMissingForm.set(false);
        this.error.set(null);
      },
      error: e => this.error.set(this.errMsg(e)),
    });
  }

  removeMissing(entry: MissingEntry) {
    if (!this.submission()?.is_editable) return;
    if (!confirm('Remove this missing-paper entry?')) return;
    this.api.deleteDecision(this.submissionId(), entry.decision_id).subscribe({
      next: () => {
        this.missing.update(list =>
          list.filter(m => m.decision_id !== entry.decision_id)
        );
      },
      error: e => this.error.set(this.errMsg(e)),
    });
  }

  submit() {
    if (this.busySubmit()) return;
    const undecided = this.summary().undecided;
    if (undecided > 0) {
      const ok = confirm(
        `You have ${undecided} paper${undecided === 1 ? '' : 's'} ` +
        `without a decision. Submit anyway?`
      );
      if (!ok) return;
    } else {
      if (!confirm('Submit your report? You won\'t be able to change decisions after this.')) {
        return;
      }
    }

    this.busySubmit.set(true);
    this.api.submitMyReport(this.submissionId()).subscribe({
      next: () => {
        this.busySubmit.set(false);
        this.refresh(this.submissionId());
      },
      error: e => {
        this.busySubmit.set(false);
        this.error.set(this.errMsg(e));
      },
    });
  }

  daysLeft(): number {
    const c = this.campaign();
    if (!c?.closes_at) return 0;
    const ms = new Date(c.closes_at).getTime() - Date.now();
    return Math.max(0, Math.ceil(ms / (1000 * 60 * 60 * 24)));
  }

  private errMsg(e: any): string {
    return e?.error?.error || e?.error?.detail || e?.message
        || 'Something went wrong';
  }
}
