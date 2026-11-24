import { Component, computed, effect, input, signal, untracked, OnDestroy } from '@angular/core';

/** A number that counts up/down to its new value instead of jumping. */
@Component({ selector: 'nova-num', template: `{{ text() }}` })
export class NumComponent implements OnDestroy {
  value = input.required<number>();
  decimals = input(0);
  currency = input(false);

  private shown = signal(0);
  private raf = 0;
  private reduce = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

  text = computed(() => {
    const d = this.decimals();
    const s = this.shown().toLocaleString(this.currency() ? 'en-IN' : undefined, {
      minimumFractionDigits: d, maximumFractionDigits: d,
    });
    return (this.currency() ? '₹' : '') + s;
  });

  constructor() {
    effect(() => {
      const to = this.value();
      untracked(() => this.animate(to));
    });
  }

  private animate(to: number) {
    cancelAnimationFrame(this.raf);
    const from = this.shown();
    if (this.reduce || from === to) { this.shown.set(to); return; }
    const t0 = performance.now(), dur = 700;
    const frame = (t: number) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);   // easeOutCubic
      this.shown.set(from + (to - from) * e);
      if (k < 1) this.raf = requestAnimationFrame(frame);
    };
    this.raf = requestAnimationFrame(frame);
  }

  ngOnDestroy() { cancelAnimationFrame(this.raf); }
}