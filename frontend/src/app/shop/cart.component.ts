import { Component, inject } from '@angular/core';
import { ShopService } from '../core/shop.service';
import { lineItem, panel } from '../shared/animations';
import { money } from '../core/models';
import { LineComponent } from './line.component';

@Component({
  selector: 'nova-cart',
  imports: [LineComponent],
  animations: [panel, lineItem],
  template: `
    @if (!shop.hold() && shop.cartLines().length) {
      <section id="cart" class="panel" @panel>
        <div class="row"><div><div class="name">Your cart · {{ shop.cartUnits() }} item{{ shop.cartUnits() === 1 ? '' : 's' }}</div>
          <div class="sub">Nothing is held until you reserve. Stock can still sell out.</div></div>
          <button class="link" (click)="shop.clearCart()">Clear cart</button></div>
        <div class="lines">
          @for (l of shop.cartLines(); track l.product_id) {
            <nova-line [line]="l" @lineItem><button class="link" (click)="shop.removeLine(l.product_id)">Remove</button></nova-line>
          }
        </div>
        <div class="total"><span>Total</span><span>{{ money(shop.cartTotal()) }}</span></div>
        <div class="actions" style="margin-top:12px">
          <button class="buy" [disabled]="!admitted() || shop.busy()" (click)="shop.reserve()">{{ label() }}</button>
        </div>
      </section>
    }
  `,
})
export class CartComponent {
  shop = inject(ShopService);
  money = money;
  admitted = () => this.shop.queue().state === 'admitted';
  label() {
    const s = this.shop.queue().state;
    return s === 'admitted' ? 'Reserve cart' : s === 'waiting' ? 'Waiting for your turn…' : 'Join the queue first';
  }
}