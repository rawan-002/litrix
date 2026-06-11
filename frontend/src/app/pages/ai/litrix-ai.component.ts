// Chatbot UI shell only — no backend yet. send() returns a canned reply so we
// can design the layout, scrolling, and input now; later swap that for the RAG
// HTTP call and the rest of the component stays as-is.
import {
  Component, signal, computed, ElementRef, viewChild, effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
}

@Component({
  selector: 'app-litrix-ai',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="flex flex-col h-[calc(100vh-7rem)] max-w-3xl mx-auto">
      <!-- Header -->
      <div class="flex items-center gap-3 pb-4 border-b border-ink-100">
        <div class="w-9 h-9 rounded-xl bg-ink-900 text-white flex items-center
                    justify-center text-lg">✦</div>
        <div>
          <h1 class="text-lg font-semibold text-ink-900 leading-tight">Litrix AI</h1>
          <p class="text-xs text-ink-400">
            Smart research assistant — soon connected to platform data (RAG)
          </p>
        </div>
      </div>

      <!-- Messages -->
      <div #scroll class="flex-1 overflow-y-auto py-6 space-y-5">
        @for (m of messages(); track $index) {
          <div class="flex" [class.justify-end]="m.role === 'user'">
            <div class="max-w-[80%] px-4 py-2.5 rounded-2xl text-sm leading-relaxed
                        whitespace-pre-wrap"
                 [class]="m.role === 'user'
                   ? 'bg-ink-900 text-white rounded-br-md'
                   : 'bg-ink-50 text-ink-800 rounded-bl-md'">
              {{ m.text }}
            </div>
          </div>
        }
        @if (thinking()) {
          <div class="flex">
            <div class="px-4 py-2.5 rounded-2xl rounded-bl-md bg-ink-50 text-ink-400 text-sm">
              …
            </div>
          </div>
        }
      </div>

      <!-- Composer -->
      <div class="pt-3 border-t border-ink-100">
        <div class="flex items-end gap-2 bg-ink-50 rounded-2xl px-3 py-2
                    focus-within:ring-2 focus-within:ring-accent/20">
          <textarea
            [(ngModel)]="draft"
            (keydown.enter)="onEnter($event)"
            rows="1"
            placeholder="Type your question…"
            class="flex-1 bg-transparent resize-none outline-none text-sm
                   text-ink-800 placeholder:text-ink-400 max-h-32 py-1.5"></textarea>
          <button
            (click)="send()"
            [disabled]="!draft.trim() || thinking()"
            class="shrink-0 w-9 h-9 rounded-xl bg-ink-900 text-white flex items-center
                   justify-center hover:bg-ink-700 transition disabled:opacity-40">
            ↑
          </button>
        </div>
        <p class="text-[11px] text-ink-300 text-center mt-2">
          Litrix AI is under development — replies are experimental.
        </p>
      </div>
    </div>
  `,
})
export class LitrixAiComponent {
  private scrollEl = viewChild<ElementRef<HTMLDivElement>>('scroll');

  readonly messages = signal<ChatMessage[]>([
    {
      role: 'assistant',
      text: 'I\'m Litrix AI. Soon I\'ll be able to answer your '
          + 'questions about papers, researchers, and stats across the '
          + 'platform. The interface is experimental for now.',
    },
  ]);
  readonly thinking = signal(false);
  draft = '';

  readonly canSend = computed(() => this.draft.trim().length > 0 && !this.thinking());

  constructor() {
    // Keep the latest message in view as the conversation grows.
    effect(() => {
      this.messages();
      this.thinking();
      queueMicrotask(() => {
        const el = this.scrollEl()?.nativeElement;
        if (el) el.scrollTop = el.scrollHeight;
      });
    });
  }

  onEnter(ev: Event) {
    const ke = ev as KeyboardEvent;
    // Enter sends; Shift+Enter inserts a newline.
    if (!ke.shiftKey) {
      ev.preventDefault();
      this.send();
    }
  }

  send() {
    const text = this.draft.trim();
    if (!text || this.thinking()) return;
    this.messages.update(m => [...m, { role: 'user', text }]);
    this.draft = '';
    this.thinking.set(true);

    // Placeholder reply. Replace this block with the RAG backend call.
    setTimeout(() => {
      this.thinking.set(false);
      this.messages.update(m => [...m, {
        role: 'assistant',
        text: 'Thanks for your question! The smart-answer feature (RAG) '
            + 'is being set up and will be available soon.',
      }]);
    }, 600);
  }
}
