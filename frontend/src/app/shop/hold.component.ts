import { Component, inject } from '@angular/core';
import { ShopService } from '../core/shop.service';
import { lineItem, panel } from '../shared/animations';
import { money, mmss } from '../core/models';
import { LineComponent } from './line.component';

@Component({
  selector: 'nova-hold',
  imports: [LineComponent],
  animations: [panel, lineItem],
  template: `
    @if (shop.hold(); as h) {
      <section id="hold" class="panel live" @panel>
        <div class="row">
          <div>
            <div class="name">Reserved for you</div>
            <div class="sub">{{ shop.order() ? 'Order created. Awaiting payment.' : 'Not checked out yet.' }}</div>
          </div>
          <div class="timer" [class.low]="shop.holdLeft() < 30">{{ mmss(shop.holdLeft()) }}</div>
        </div>
        <div class="lines">
          @for (l of h.items; track l.product_id) { <nova-line [line]="l" @lineItem /> }
        </div>
        <div class="total"><span>Total</span><span>{{ money(shop.order() ? shop.order()!.amount_cents : shop.linesTotal(h.items)) }}</span></div>
        <div class="actions" style="margin-top:14px">
          @if (shop.order()) {
            @if (shop.payments().provider === 'razorpay') {
              <button class="buy" [disabled]="shop.busy()" (click)="shop.holdAction('rzp')">Pay with Razorpay</button>
            } @else {
              <button class="buy" [disabled]="shop.busy()" (click)="shop.holdAction('pay')">Pay now</button>
              <button class="danger" [disabled]="shop.busy()" (click)="shop.holdAction('decline')">Simulate declined card</button>
            }
            <button class="ghost" [disabled]="shop.busy()" (click)="shop.holdAction('cancel')">Release</button>
          } @else {
            <button class="buy" [disabled]="shop.busy()" (click)="shop.holdAction('checkout')">Checkout</button>
            <button class="ghost" [disabled]="shop.busy()" (click)="shop.holdAction('cancel')">Release</button>
          }
        </div>
      </section>
    }
  `,
})
export class HoldComponent {
  shop = inject(ShopService);
  money = money;
  mmss = mmss;
}