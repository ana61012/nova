import { Component, inject } from '@angular/core';
import { ShopService } from '../core/shop.service';
import { roll } from '../shared/animations';
import { mmss } from '../core/models';

@Component({
  selector: 'nova-queue',
  animations: [roll],
  template: `
    @let q = shop.queue();
    <section class="queue" id="queue" [class.in]="q.state === 'admitted'">
      @switch (q.state) {
        @case ('waiting') {
          <div class="swap-in"><div class="sub">You are in line. Build your cart while you wait.</div>
            <div class="big"><span class="roll-num" [@roll]="q.position">#{{ q.position }}</span></div>
            <div class="sub">{{ q.waiting_total }} waiting in total. Leaving or reloading this page signs you out.</div></div>
          <button class="ghost swap-in" (click)="shop.queueAction('leave')">Leave line</button>
        }
        @case ('admitted') {
          <div class="swap-in"><div class="sub">You're in. Reserve your cart</div>
            <div class="big">{{ mmssLeft() }} left to shop</div></div>
          <button class="ghost swap-in" (click)="shop.queueAction('leave')">Done shopping</button>
        }
        @case ('expired') {
          <div class="swap-in"><div class="sub">Your turn ran out</div><div class="big">Missed your window</div></div>
          <button class="buy swap-in" (click)="shop.queueAction('join')">Rejoin at the back</button>
        }
        @default {
          <div class="swap-in"><div class="sub">{{ q.state === 'done' ? "You're done for this drop" : 'The drop is live' }}</div>
            <div class="big">{{ q.state === 'done' ? 'Thanks for shopping' : 'Join the queue for a turn' }}</div></div>
          <button class="buy swap-in" (click)="shop.queueAction('join')">{{ q.state === 'done' ? 'Get back in line' : 'Join queue' }}</button>
        }
      }
    </section>
  `,
})
export class QueueComponent {
  shop = inject(ShopService);
  mmssLeft() {
    const s = this.shop.queueLeft() ?? 0;
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
}