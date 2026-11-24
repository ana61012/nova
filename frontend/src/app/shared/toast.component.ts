import { Component, computed, inject } from '@angular/core';
import { ToastService } from '../core/toast.service';
import { toastAnim } from './animations';

@Component({
  selector: 'nova-toast',
  animations: [toastAnim],
  template: `
    @for (t of items(); track t.id) {
      <div id="toast" class="show" [class.err]="t.err" @toastAnim role="status">{{ t.msg }}</div>
    }
  `,
})
export class ToastComponent {
  private toast = inject(ToastService);
  items = computed(() => { const t = this.toast.current(); return t ? [t] : []; });
}