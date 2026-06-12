// One-shot celebration overlay: a full-screen canvas of fireworks plus a
// welcome card. Pure canvas, no libraries. Auto-dismisses (or on click).
// A small easter-egg greeting on the dashboard.
import {
  Component, ElementRef, OnDestroy, AfterViewInit, viewChild, signal, input, output,
} from '@angular/core';

interface Particle {
  x: number; y: number; vx: number; vy: number;
  life: number; maxLife: number; color: string; size: number;
}

@Component({
  selector: 'app-fireworks',
  standalone: true,
  template: `
    @if (visible()) {
      <div class="fixed inset-0 z-[60] flex items-center justify-center"
           (click)="dismiss()">
        <canvas #cv class="absolute inset-0 w-full h-full pointer-events-none"></canvas>

        <div class="relative text-center px-8 py-7 rounded-3xl bg-white/85 backdrop-blur-md
                    shadow-hover border border-white/60 max-w-md mx-4 animate-[pop_.4s_ease-out]"
             (click)="$event.stopPropagation()">
          <div class="text-4xl mb-2">🎉</div>
          <h2 class="text-xl font-bold text-ink-900 mb-1">
            Welcome, {{ name() }}
          </h2>
          <p class="text-sm text-ink-600 leading-relaxed">
            We're glad to have you on <span class="font-semibold text-ink-900">Litrix</span> -
            thank you for your guidance and trust 💚
          </p>
          <button (click)="dismiss()"
                  class="mt-5 text-xs font-medium text-ink-500 hover:text-ink-900 transition-colors">
            Skip
          </button>
        </div>
      </div>
    }
    <style>
      @keyframes pop { 0% { transform: scale(.85); opacity: 0 } 100% { transform: scale(1); opacity: 1 } }
    </style>
  `,
})
export class FireworksComponent implements AfterViewInit, OnDestroy {
  /** Display name shown in the greeting. */
  readonly name = input<string>('');
  /** Auto-dismiss after this many ms. */
  readonly durationMs = input<number>(8000);
  readonly closed = output<void>();

  private readonly cv = viewChild<ElementRef<HTMLCanvasElement>>('cv');
  readonly visible = signal(true);

  private ctx: CanvasRenderingContext2D | null = null;
  private particles: Particle[] = [];
  private raf = 0;
  private timers: any[] = [];
  private readonly COLORS = ['#0071e3', '#34c759', '#ff375f', '#ffd60a', '#bf5af2', '#ff9f0a'];

  ngAfterViewInit(): void {
    const canvas = this.cv()?.nativeElement;
    if (!canvas) return;
    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    };
    resize();
    window.addEventListener('resize', resize);
    this.timers.push(() => window.removeEventListener('resize', resize));

    this.ctx = canvas.getContext('2d');
    this.loop();

    // Launch a burst every ~600ms for the first stretch of the show.
    const launcher = setInterval(() => this.launch(canvas.width, canvas.height), 600);
    this.timers.push(() => clearInterval(launcher));
    // A few immediate bursts so it kicks off lively.
    this.launch(canvas.width, canvas.height);
    setTimeout(() => this.launch(canvas.width, canvas.height), 200);

    const auto = setTimeout(() => this.dismiss(), this.durationMs());
    this.timers.push(() => clearTimeout(auto));
  }

  private launch(w: number, h: number): void {
    const cx = w * (0.2 + Math.random() * 0.6);
    const cy = h * (0.2 + Math.random() * 0.4);
    const color = this.COLORS[Math.floor(Math.random() * this.COLORS.length)];
    const count = 38 + Math.floor(Math.random() * 26);
    for (let i = 0; i < count; i++) {
      const angle = (Math.PI * 2 * i) / count;
      const speed = 2 + Math.random() * 4;
      this.particles.push({
        x: cx, y: cy,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        life: 0, maxLife: 60 + Math.random() * 30,
        color, size: 1.5 + Math.random() * 1.5,
      });
    }
  }

  private loop = (): void => {
    const ctx = this.ctx;
    const canvas = this.cv()?.nativeElement;
    if (!ctx || !canvas) return;

    // Trailing fade for a glowing effect.
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.globalCompositeOperation = 'lighter';

    this.particles = this.particles.filter(p => p.life < p.maxLife);
    for (const p of this.particles) {
      p.life++;
      p.x += p.vx;
      p.y += p.vy;
      p.vy += 0.05;          // gravity
      p.vx *= 0.99;          // drag
      p.vy *= 0.99;
      const alpha = 1 - p.life / p.maxLife;
      ctx.globalAlpha = Math.max(0, alpha);
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    this.raf = requestAnimationFrame(this.loop);
  };

  dismiss(): void {
    if (!this.visible()) return;
    this.visible.set(false);
    this.cleanup();
    this.closed.emit();
  }

  private cleanup(): void {
    cancelAnimationFrame(this.raf);
    this.timers.forEach(fn => fn());
    this.timers = [];
    this.particles = [];
  }

  ngOnDestroy(): void {
    this.cleanup();
  }
}
