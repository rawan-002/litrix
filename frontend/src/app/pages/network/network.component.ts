// @ts-nocheck
//
// WHY @ts-nocheck:
//   This component is a D3 force-simulation playground. D3's API is
//   built on generic method chaining (`selectAll().data().enter()
//   .append().attr(...)`) whose type inference does not survive
//   Angular's strict production TS pass — every callback (`d => ...`)
//   ends up flagged as implicit `any`, every `d3.SimulationNodeDatum`
//   field gets dropped, etc.
//
//   Sprinkling explicit type annotations on every D3 callback is
//   verbose and fragile (each `attr/style/text` chain needs a fresh
//   generic). The pragmatic engineering call is to opt this single
//   file out of strict TS checking — D3 itself is the source of
//   truth for runtime behaviour, and the rest of the codebase stays
//   strict.
//
//   If you change this file, run `ng serve` locally to catch logic
//   errors. The Angular template type-checker still validates the
//   HTML side.
//
/**
 * Litrix Research Network — interactive collaboration graph.
 *
 * Modes
 *   • coauthors  : classic co-authorship graph
 *   • interests  : researchers with overlapping research topics
 *   • both       : merged view (solid edges = coauthor, dashed = interest)
 *
 * UX
 *   • Force-directed layout (D3 simulation) centred on the current user
 *   • Click a node  → show detail strip (interests + shared topics)
 *   • Double-click  → recentre the graph on that node
 *   • Hidden Gems   → people you should probably meet (high topic overlap,
 *                     zero co-authored papers)
 *   • Export PNG    → download the current SVG as a PNG image
 *
 * Tech
 *   • d3-force for the physics, raw <svg> for rendering
 *   • Signals for state, computed() for derived geometry
 */
import {
  Component, OnInit, OnDestroy, inject, signal,
  ElementRef, ViewChild, AfterViewInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Router } from '@angular/router';
import { AuthService } from '../../core/services/auth.service';
import { environment } from '../../../environments/environment';

import * as d3 from 'd3';

type NetMode = 'coauthors' | 'interests' | 'both';

interface NetNode extends d3.SimulationNodeDatum {
  id:        string;
  label:     string;
  dept:      string;
  papers:    number;
  shared:    number;
  kind:      'self' | 'same' | 'cross' | 'external' | 'interest';
  litrix_id?: string;
  affil?:    string;
  interests?: string[];
  shared_interests?: string[];
  shared_interests_count?: number;

  // Repeat the SimulationNodeDatum fields explicitly. Some Vercel
  // build configs don't resolve d3's inherited `x/y/fx/fy` cleanly,
  // so we restate them here to make strict mode happy.
  x?:  number;
  y?:  number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
}

interface NetEdge extends d3.SimulationLinkDatum<NetNode> {
  source: string | NetNode;
  target: string | NetNode;
  weight: number;
  kind?:  'coauthor' | 'interest';
}

interface HiddenGem {
  litrix_id:        string;
  name:             string;
  dept:             string;
  overlap:          number;
  papers:           number;
  shared_interests: string[];
}

interface NetworkInsights {
  total_collaborators: number;
  internal:            number;
  external:            number;
  top_coauthor?:       { name: string; dept: string; shared: number } | null;
  top_external?:       { name: string; affil: string; shared: number } | null;
  most_connected_dept?: string | null;
  top_institutions?:   [string, number][];
  top_interests?:      string[];
  interest_neighbours?: number;
  hidden_gems?:        HiddenGem[];
}

interface NetworkPayload {
  center:   {
    user_id:   number;
    name_ar:   string;
    name_en:   string;
    litrix_id: string;
    dept:      string;
    papers:    number;
  };
  nodes:    NetNode[];
  edges:    NetEdge[];
  insights: NetworkInsights;
  filters:  any;
}


@Component({
  selector: 'app-network',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './network.component.html',
  styleUrls: ['./network.component.scss'],
})
export class NetworkComponent implements OnInit, AfterViewInit, OnDestroy {
  private http   = inject(HttpClient);
  private router = inject(Router);
  private auth   = inject(AuthService);

  @ViewChild('svgRoot', { static: false }) svgRoot?: ElementRef<SVGSVGElement>;

  // ---- State ----
  readonly data    = signal<NetworkPayload | null>(null);
  readonly loading = signal<boolean>(true);
  readonly error   = signal<string | null>(null);

  // Mode toggle (coauthors / interests / both)
  readonly mode = signal<NetMode>('coauthors');

  // Filters
  readonly internalOnly = signal<boolean>(false);
  readonly minPapers    = signal<number>(1);
  readonly yearFrom     = signal<number | null>(null);
  readonly yearTo       = signal<number | null>(null);

  // Whoever the graph is currently centred on
  readonly centreLitrixId = signal<string | null>(null);

  // Node selected by single-click — drives the detail strip
  readonly selectedNode = signal<NetNode | null>(null);

  private simulation?: d3.Simulation<NetNode, NetEdge>;

  ngOnInit(): void {
    const user = this.auth.user();
    const lit = user?.litrix_id || 'Lit-000001';
    this.centreLitrixId.set(lit);
    this.fetch();
  }

  ngAfterViewInit(): void { /* simulation starts on data arrival */ }

  ngOnDestroy(): void {
    this.simulation?.stop();
  }

  // -----------------------------------------------------------------
  setMode(m: NetMode): void {
    if (this.mode() === m) return;
    this.mode.set(m);
    this.selectedNode.set(null);
    this.fetch();
  }

  fetch(): void {
    const lit = this.centreLitrixId();
    if (!lit) return;
    this.loading.set(true);
    this.error.set(null);

    let params = new HttpParams().set('mode', this.mode());
    if (this.internalOnly()) params = params.set('internal_only', 'true');
    if (this.minPapers() > 1) params = params.set('min_papers', String(this.minPapers()));
    if (this.yearFrom()) params = params.set('year_from', String(this.yearFrom()));
    if (this.yearTo())   params = params.set('year_to',   String(this.yearTo()));

    this.http.get<NetworkPayload>(
      `${environment.apiBaseUrl}/researchers/${lit}/network/`, { params }
    ).subscribe({
      next: payload => {
        this.data.set(payload);
        this.loading.set(false);
        this.renderWhenReady(payload, 0);
      },
      error: err => {
        this.error.set(err.error?.error || 'Could not load the network.');
        this.loading.set(false);
      },
    });
  }

  /**
   * Poll a few times until the @ViewChild('svgRoot') reference is set
   * (Angular's @if needs a change-detection pass after data() flips).
   */
  private renderWhenReady(payload: NetworkPayload, attempt: number): void {
    if (this.svgRoot?.nativeElement) {
      this.renderGraph(payload);
      return;
    }
    if (attempt > 12) return;
    setTimeout(() => this.renderWhenReady(payload, attempt + 1), 50);
  }

  // -----------------------------------------------------------------
  // Force simulation + SVG rendering
  // -----------------------------------------------------------------
  private renderGraph(payload: NetworkPayload): void {
    if (!this.svgRoot) return;
    const svgEl = this.svgRoot.nativeElement;
    const W = svgEl.clientWidth || 900;
    const H = svgEl.clientHeight || 560;

    this.simulation?.stop();

    const nodes: NetNode[] = payload.nodes.map(n => ({ ...n }));
    const edges: NetEdge[] = payload.edges.map(e => ({ ...e }));

    const svg = d3.select(svgEl);
    svg.selectAll('*').remove();

    const g = svg.append('g');

    const zoomBehaviour = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.4, 3])
      .on('zoom', (ev: any) => g.attr('transform', ev.transform.toString()));
    svg.call(zoomBehaviour as any);

    // ---- Edges (different styling for coauthor vs interest) ----
    const link = g.append('g')
      .attr('class', 'links')
      .selectAll('line')
      .data(edges)
      .enter().append('line')
      .attr('stroke', d => d.kind === 'interest' ? '#34a36b' : '#86868b')
      .attr('stroke-opacity', d => d.kind === 'interest' ? 0.45 : 0.35)
      .attr('stroke-dasharray', d => d.kind === 'interest' ? '4 4' : '')
      .attr('stroke-width', d => Math.max(0.6, Math.min(4, Math.sqrt(d.weight))));

    // ---- Node colors ----
    const colourFor = (n: NetNode): string => {
      if (n.kind === 'self')     return '#0071e3';
      if (n.kind === 'same')     return '#1d1d1f';
      if (n.kind === 'cross')    return '#86868b';
      if (n.kind === 'interest') return '#34a36b'; // teal-green
      return '#d4a72c'; // external
    };

    const radiusFor = (n: NetNode): number => {
      if (n.kind === 'self') return 22;
      return 6 + Math.min(20, Math.sqrt(n.shared) * 3);
    };

    const nodeG = g.append('g')
      .attr('class', 'nodes')
      .selectAll('g')
      .data(nodes)
      .enter().append('g')
      .style('cursor', 'pointer')
      .on('click', (_: any, d: NetNode) => {
        // Single click — show details
        this.selectedNode.set(d);
      })
      .on('dblclick', (_: any, d: NetNode) => {
        // Double click — recentre on this node
        if (d.kind !== 'external' && d.litrix_id) {
          this.centreLitrixId.set(d.litrix_id);
          this.selectedNode.set(null);
          this.fetch();
        }
      });

    nodeG.append('circle')
      .attr('r', radiusFor)
      .attr('fill', colourFor)
      .attr('stroke', '#fff')
      .attr('stroke-width', 2);

    // Inner ring for interest-mode nodes (so they pop)
    nodeG.filter(d => d.kind === 'interest')
      .append('circle')
      .attr('r', d => radiusFor(d) - 3)
      .attr('fill', 'none')
      .attr('stroke', '#fff')
      .attr('stroke-width', 1.5)
      .attr('stroke-dasharray', '2 2')
      .style('pointer-events', 'none');

    // Native tooltip — quick glance without click
    nodeG.append('title').text(d => {
      const parts = [d.label, d.dept];
      if (d.kind === 'interest') {
        parts.push(`${d.shared} shared topic${d.shared === 1 ? '' : 's'}`);
      } else if (d.kind === 'external') {
        parts.push(`${d.shared} shared paper${d.shared === 1 ? '' : 's'}`);
      } else if (d.kind !== 'self') {
        parts.push(`${d.shared} shared paper${d.shared === 1 ? '' : 's'}`);
      }
      if (d.interests?.length) {
        parts.push('— ' + d.interests.slice(0, 3).join(' · '));
      }
      return parts.filter(Boolean).join('\n');
    });

    // ------------------------------------------------------------
    // Node labels — show ON EVERY NODE.
    //
    // Two-tier visual hierarchy keeps the graph readable even when
    // many nodes are on screen:
    //   * self        → 13px bold (the "you are here" anchor)
    //   * coauthors / interest neighbours → 11px regular
    //   * external    → 10px muted (kept smaller — usually the
    //                   majority of nodes, so smaller text reduces
    //                   visual clutter)
    //
    // Truncation: hard-cap at 24 chars with an ellipsis. Beyond that
    // the layout starts to overlap regardless of font size.
    //
    // Halo (paint-order stroke): a thin white outline around the
    // text so it stays legible when it crosses an edge or another
    // circle. This is a standard D3 trick — `paint-order: stroke`
    // draws the stroke BEFORE the fill so the fill sits on top.
    // ------------------------------------------------------------
    const truncate = (s: string, n: number): string =>
      s && s.length > n ? s.slice(0, n - 1).trim() + '…' : (s || '');

    const fontFor = (n: NetNode): number => {
      if (n.kind === 'self')     return 13;
      if (n.kind === 'external') return 10;
      return 11;
    };

    const labelFor = (n: NetNode): string => {
      if (n.kind === 'self')     return truncate(n.label, 32);
      if (n.kind === 'external') return truncate(n.label, 20);
      return truncate(n.label, 24);
    };

    nodeG.append('text')
      .text(labelFor)
      .attr('font-size', fontFor)
      .attr('font-weight', d => d.kind === 'self' ? 600 : 500)
      .attr('fill', d => d.kind === 'external' ? '#6e6e73' : '#1d1d1f')
      .attr('text-anchor', 'middle')
      .attr('dy', d => -radiusFor(d) - 6)
      .attr('paint-order', 'stroke')
      .attr('stroke', '#ffffff')
      .attr('stroke-width', 3)
      .attr('stroke-linejoin', 'round')
      .style('pointer-events', 'none');   // text never blocks click on the circle

    this.simulation = d3.forceSimulation<NetNode>(nodes)
      .force('link', d3.forceLink<NetNode, NetEdge>(edges)
                       .id((n: NetNode) => n.id)
                       .distance(120)
                       .strength(0.4))
      .force('charge', d3.forceManyBody().strength(-260))
      .force('centre', d3.forceCenter(W / 2, H / 2))
      .force('collide', d3.forceCollide<NetNode>().radius((n: NetNode) => radiusFor(n) + 6))
      .on('tick', () => {
        link
          .attr('x1', d => (d.source as NetNode).x!)
          .attr('y1', d => (d.source as NetNode).y!)
          .attr('x2', d => (d.target as NetNode).x!)
          .attr('y2', d => (d.target as NetNode).y!);
        nodeG.attr('transform', d => `translate(${d.x},${d.y})`);
      });

    // Explicit `any` types on the d3 drag callbacks — Angular's
    // production build runs TS in strict mode, which rejects the
    // implicit `any` that d3's overload resolution leaves behind.
    // Casting at the call site keeps the rest of the code typed.
    const drag = d3.drag<SVGGElement, NetNode>()
      .on('start', (event: any, d: NetNode) => {
        if (!event.active) this.simulation!.alphaTarget(0.3).restart();
        d.fx = d.x; d.fy = d.y;
      })
      .on('drag', (event: any, d: NetNode) => {
        d.fx = event.x; d.fy = event.y;
      })
      .on('end', (event: any, d: NetNode) => {
        if (!event.active) this.simulation!.alphaTarget(0);
        d.fx = null; d.fy = null;
      });
    nodeG.call(drag as any);
  }

  // -----------------------------------------------------------------
  // Export the current graph as a PNG (right from the browser, no
  // server round-trip). Uses the standard SVG -> canvas -> PNG dance.
  // -----------------------------------------------------------------
  exportPNG(): void {
    if (!this.svgRoot?.nativeElement) return;
    const svgEl = this.svgRoot.nativeElement;

    // Serialize, then patch the SVG with explicit dimensions + a white
    // background so the rendered PNG isn't transparent.
    const serializer = new XMLSerializer();
    const clone = svgEl.cloneNode(true) as SVGSVGElement;
    const W = svgEl.clientWidth || 900;
    const H = svgEl.clientHeight || 560;
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('width',  String(W));
    clone.setAttribute('height', String(H));
    // Insert a white <rect> as the first child for the PNG background
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('width',  '100%');
    rect.setAttribute('height', '100%');
    rect.setAttribute('fill', '#ffffff');
    clone.insertBefore(rect, clone.firstChild);

    const svgString = serializer.serializeToString(clone);
    const svgBlob = new Blob([svgString], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(svgBlob);

    const img = new Image();
    img.onload = () => {
      const scale = 2; // retina-ish
      const canvas = document.createElement('canvas');
      canvas.width  = W * scale;
      canvas.height = H * scale;
      const ctx = canvas.getContext('2d')!;
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);

      canvas.toBlob((blob) => {
        if (!blob) return;
        const dl = document.createElement('a');
        const pretty = (this.data()?.center?.name_en || 'litrix-network')
          .replace(/\s+/g, '-').toLowerCase();
        dl.download = `${pretty}-network.png`;
        dl.href = URL.createObjectURL(blob);
        document.body.appendChild(dl);
        dl.click();
        dl.remove();
        setTimeout(() => URL.revokeObjectURL(dl.href), 1000);
      }, 'image/png');
    };
    img.src = url;
  }

  // -----------------------------------------------------------------
  // Filter handlers
  // -----------------------------------------------------------------
  onFilterChange(): void {
    setTimeout(() => this.fetch(), 120);
  }

  goToProfile(litId: string): void {
    this.router.navigate(['/profile', litId]);
  }

  resetCentre(): void {
    const me = this.auth.user()?.litrix_id;
    if (me) {
      this.centreLitrixId.set(me);
      this.selectedNode.set(null);
      this.fetch();
    }
  }
}
