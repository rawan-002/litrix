/**
 * CitationsChart — the single, shared "Citations over Time" chart.
 *
 * Previously every dashboard hand-rolled its own SVG: the admin overview
 * used an area/line trend, while the researcher dashboard and profile used
 * bars of slightly different heights. This component is the one canonical
 * area/line implementation (matching the admin look) so the chart is
 * visually identical for Admin, Dean, HoD, and Researcher.
 *
 * Drop it inside any card; it renders only the chart body (no title), owns
 * its own hover state, and scales to the host width.
 *
 * Usage:
 *   <app-citations-chart [series]="citationsByYear" [height]="200" />
 * where `series` is `{ year, citations }[]`.
 */
import { Component, computed, input, signal } from '@angular/core';
import { CommonModule } from '@angular/common';

export interface YearCitations {
  year: number;
  citations: number;
}

interface ChartPoint { x: number; y: number; year: number; value: number; }

@Component({
  selector: 'app-citations-chart',
  standalone: true,
  imports: [CommonModule],
  template: `
    @if (geometry(); as c) {
      <svg [attr.viewBox]="'0 0 ' + c.W + ' ' + c.H"
           class="w-full h-auto select-none" preserveAspectRatio="xMidYMid meet"
           (mouseleave)="hovered.set(null)">

        <defs>
          <linearGradient [attr.id]="gradId" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"   stop-color="#1c1917" stop-opacity="0.20" />
            <stop offset="100%" stop-color="#1c1917" stop-opacity="0.02" />
          </linearGradient>
        </defs>

        @for (yl of c.yLabels; track yl.v) {
          <text [attr.x]="c.padding.left - 8" [attr.y]="yl.y + 4"
                text-anchor="end" class="text-[10px] fill-ink-400">
            {{ yl.v }}
          </text>
          <line [attr.x1]="c.padding.left" [attr.x2]="c.W - c.padding.right"
                [attr.y1]="yl.y" [attr.y2]="yl.y"
                stroke="#f5f5f4" stroke-width="1" />
        }

        <path [attr.d]="c.areaPath" [attr.fill]="'url(#' + gradId + ')'" />
        <path [attr.d]="c.linePath"
              fill="none" stroke="#1c1917" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round" />

        @for (p of c.points; track p.year) {
          <g (mouseenter)="hovered.set(p)">
            <circle [attr.cx]="p.x" [attr.cy]="p.y" r="14" fill="transparent" />
            <circle [attr.cx]="p.x" [attr.cy]="p.y"
                    [attr.r]="hovered()?.year === p.year ? 6 : 4"
                    fill="white"
                    [attr.stroke]="hovered()?.year === p.year ? '#0071e3' : '#1c1917'"
                    [attr.stroke-width]="hovered()?.year === p.year ? 3 : 2"
                    class="transition-all" />
            <text [attr.x]="p.x" [attr.y]="p.y - 12"
                  text-anchor="middle"
                  class="text-[10px] font-medium pointer-events-none transition-colors"
                  [class.fill-accent]="hovered()?.year === p.year"
                  [class.fill-ink-700]="!hovered() || hovered()?.year === p.year"
                  [class.fill-ink-400]="hovered() && hovered()?.year !== p.year">
              {{ p.value }}
            </text>
            <text [attr.x]="p.x" [attr.y]="c.H - c.padding.bottom + 16"
                  text-anchor="middle" class="text-[10px] fill-ink-500">
              {{ p.year }}
            </text>
          </g>
        }

        @if (hovered(); as h) {
          <g [attr.transform]="'translate(' + h.x + ',' + (h.y - 16) + ')'"
             class="pointer-events-none">
            <rect x="-38" y="-26" width="76" height="22" rx="4" fill="#0071e3" />
            <text text-anchor="middle" y="-11"
                  class="text-[10px] fill-white font-semibold">
              {{ h.year }} · {{ h.value }} cit.
            </text>
          </g>
        }
      </svg>
    } @else {
      <div class="text-center py-12 text-sm text-ink-400">No citations data</div>
    }
  `,
})
export class CitationsChartComponent {
  /** `{ year, citations }` pairs, any order. */
  readonly series = input<YearCitations[]>([]);
  /** Chart height in SVG units (width is fixed at 500 and scales to host). */
  readonly height = input<number>(200);
  /** Clip years before this for a compact, recent-growth axis. */
  readonly floorYear = input<number>(2019);

  readonly hovered = signal<ChartPoint | null>(null);

  // A stable-enough gradient id so multiple instances on one page don't
  // collide. (Date/random are avoided; height+length is enough here.)
  readonly gradId = `citGrad_${Math.round(Math.random() * 1e6)}`;

  readonly geometry = computed(() => {
    const series = [...this.series()]
      .filter(s => s && s.year >= this.floorYear())
      .sort((a, b) => a.year - b.year);
    if (!series.length) return null;

    const W = 500, H = this.height(),
          padding = { top: 16, right: 16, bottom: 32, left: 36 };
    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const max    = Math.max(...series.map(s => s.citations), 1);

    const xStep = series.length > 1 ? innerW / (series.length - 1) : 0;

    const points: ChartPoint[] = series.map((s, i) => ({
      x: padding.left + i * xStep,
      y: padding.top + innerH - (s.citations / max) * innerH,
      year: s.year,
      value: s.citations,
    }));

    const linePath = points
      .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)},${p.y.toFixed(1)}`)
      .join(' ');

    const baseline = padding.top + innerH;
    const first = points[0];
    const last  = points[points.length - 1];
    const areaPath =
      `${linePath} L ${last.x.toFixed(1)},${baseline} ` +
      `L ${first.x.toFixed(1)},${baseline} Z`;

    return {
      W, H, padding, points, linePath, areaPath,
      yLabels: [
        { v: max,                              y: padding.top },
        { v: Math.max(1, Math.round(max / 2)), y: padding.top + innerH / 2 },
        { v: 0,                                y: padding.top + innerH },
      ],
    };
  });
}
