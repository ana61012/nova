import { Component, OnDestroy, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ShopService } from '../core/shop.service';
import { AuthComponent } from './auth.component';
import { StageComponent } from './stage.component';
import { QueueComponent } from './queue.component';
import { HoldComponent } from './hold.component';
import { CartComponent } from './cart.component';
import { GridComponent } from './grid.component';
import { LineComponent } from './line.component';
import { panel } from '../shared/animations';

@Component({
  selector: 'nova-shop',
  imports: [RouterLink, AuthComponent, StageComponent, QueueComponent, HoldComponent, CartComponent, GridComponent, LineComponent],
  animations: [panel],
  template: `
    <header class="top">
      <a class="brand" routerLink="/">nova</a>
      @if (shop.session(); as s) {
        <div class="pill swap-in"><span>{{ s.email }}</span><button (click)="shop.logout()">Log out</button></div>
      }
    </header>

    <main>
      <!-- collection slider: shown signed out and signed in -->
      <nova-stage />

      @if (!shop.session()) {
        <nova-auth class="swap-in" />
      } @else {
        <div class="swap-in">
          <nova-queue />
          <nova-hold />
          @if (shop.paid(); as p) {
            <section id="done" class="panel live" @panel>
              <div class="row"><div class="sub">Order confirmed. A confirmation email is on its way.</div>
                <button class="ghost" (click)="shop.dismissPaid()">Dismiss</button></div>
              <div class="lines">
                @for (l of p.items; track l.product_id) { <nova-line [line]="l" /> }
              </div>
              <div class="total"><span>Paid</span><span>{{ money(p.amount_cents) }}</span></div>
            </section>
          }
          <nova-cart />
          <nova-grid />
        </div>
      }
    </main>

    <footer>nova · <a routerLink="/dashboard">live dashboard</a> ·
      <span>{{ shop.payments().provider === 'razorpay' ? 'payments by Razorpay' : 'payments are simulated' }}</span></footer>
  `,
})
export class ShopComponent implements OnDestroy {
  shop = inject(ShopService);
  money = (c: number) => '₹' + (c / 100).toLocaleString('en-IN');

  /** Leaving the page signs you out, as it always has (sessions live in memory only). */
  ngOnDestroy() { this.shop.logout(); }
}