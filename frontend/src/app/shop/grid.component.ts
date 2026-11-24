import { Component, inject } from '@angular/core';
import { ShopService } from '../core/shop.service';
import { CLOTHES, Product, money } from '../core/models';
import { bump } from '../shared/animations';

@Component({
  selector: 'nova-grid',
  animations: [bump],
  template: `
    <div class="intro">
      <h2>{{ shop.dropName() }}</h2>
      <div class="sub">Limited stock. Fair queue. Holds expire. No oversells.</div>
    </div>
    <div id="grid" class="grid">
      @for (p of shop.products(); track p.id; let i = $index) {
        @let qty = shop.cart()[p.id] || 0;
        @let max = shop.maxFor(p);
        @let out = p.available === 0;
        <div [class]="'card card-in tone-' + p.image_emoji" [class.in-cart]="qty > 0" [class.sold-out]="out" [style.--i]="i">
          <div class="frame">
            @if (kind(p); as k) {
              <img [class.cut]="k === 'cut'" [src]="'/static/clothes/' + p.image_emoji + '.webp'" [alt]="p.name" loading="lazy">
            }
          </div>
          <div class="meta"><div class="name">{{ p.name }}</div><div class="price">{{ money(p.price_cents) }}</div></div>
          <div class="bar"><i [style.width.%]="(p.available / p.total) * 100" [style.background]="color(p)"></i></div>
          <div class="stock"><span>{{ out ? 'Sold out' : p.available + ' left' }}</span><span>max {{ shop.limits().max_qty_per_product }} each</span></div>
          <div class="stepper">
            <button [disabled]="!!shop.hold() || qty === 0" (click)="shop.stepCart(p.id, -1)" aria-label="Remove one">−</button>
            <span class="qty" [@bump]="qty">{{ qty }}</span>
            <button [disabled]="!!shop.hold() || out || qty >= max" (click)="shop.stepCart(p.id, 1)" aria-label="Add one">+</button>
          </div>
        </div>
      }
    </div>
  `,
})
export class GridComponent {
  shop = inject(ShopService);
  money = money;
  kind = (p: Product) => CLOTHES[p.image_emoji];
  color(p: Product) {
    const pct = (p.available / p.total) * 100;
    return pct < 25 ? 'var(--danger)' : pct < 50 ? 'var(--warn)' : 'var(--ok)';
  }
}