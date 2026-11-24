import { Injectable, signal } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class ToastService {
  readonly current = signal<{ id: number; msg: string; err: boolean } | null>(null);
  private n = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;

  show(msg: string, err = false) {
    clearTimeout(this.timer);
    this.current.set({ id: ++this.n, msg, err });
    this.timer = setTimeout(() => this.current.set(null), 3200);
  }
}