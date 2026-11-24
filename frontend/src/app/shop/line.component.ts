import { Component, inject, input } from '@angular/core';
import { ShopService } from '../core/shop.service';
import { CLOTHES, Line, money } from '../core/models';

/** One "thumb · name × qty · price" row, shared by cart, hold and receipt. */
@Component({
  selector: 'nova-line',
  template: `
    @let p = shop.productOf(line().product_id);
    <span class="l">
      @if (p && clothes[p.image_emoji]) { <img class="thumb" [src]="'/static/clothes/' + p.image_emoji + '.webp'" alt=""> }
      <span>{{ p ? p.name : 'Item' }} × {{ line().qty }}</span>
      <ng-content />
    </span>
    <span>{{ p ? money(p.price_cents * line().qty) : '' }}</span>
  `,
  host: { class: 'line' },
})
export class LineComponent {
  shop = inject(ShopService);
  line = input.required<Line>();
  clothes = CLOTHES;
  money = money;
}