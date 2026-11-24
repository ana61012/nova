import { Component, HostListener, OnDestroy, computed, effect, inject, signal, untracked } from '@angular/core';
import { ShopService } from '../core/shop.service';
import { BLURB, CLOTHES, STATIC_SLIDES, Slide, money } from '../core/models';
import { bump, roll } from '../shared/animations';

/** The collection slider. Slides travel at different speeds (--d drives every layer). */
@Component({
  selector: 'nova-stage',
  animations: [bump, roll],
  template: `
    <section class="stage" id="stage" aria-roledescription="carousel" aria-label="The collection"
             [class.single]="n() < 2" (pointerdown)="down($event)" (pointerup)="up($event)">
      <div id="stageTrack">
        @for (s of list(); track key(s); let i = $index) {
          <article [class]="'slide tone-' + s.image_emoji" [class.on]="i === cur()"
                   [style.--d]="dist(i).toFixed(4)" [style.visibility]="vis(i)" [attr.inert]="i === cur() ? null : ''">
            <div class="mark" aria-hidden="true">{{ s.name }}</div>
            <div class="echo" aria-hidden="true"><img [src]="img(s)" alt=""></div>
            <div class="pic"><div class="arch"><img [class.cut]="kind(s) === 'cut'" [src]="img(s)" [alt]="s.name"></div></div>
            <div class="copy">
              <div class="reveal"><h2>{{ s.name }}</h2></div>
              <div class="reveal"><p>{{ blurb(s) }}</p></div>
              <div class="reveal"><div class="priceline"><span>{{ s.price_cents != null ? money(s.price_cents) : 'Small run' }}</span><span>{{ stock(s) }}</span></div></div>
              <div class="reveal"><button class="buy" type="button" [disabled]="disabled(s)" [@bump]="qty(s)" (click)="add(s)">{{ label(s) }}</button></div>
            </div>
          </article>
        }
      </div>

      @if (shop.session()) {
        <button class="bag" id="bag" type="button" aria-label="Go to your cart" (click)="goBag()">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 8h14l-1 12H6L5 8z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg>
          <span id="bagN" [@bump]="shop.bagCount()">{{ shop.bagCount() }}</span>
        </button>
      }

      <div class="tab" id="stageTab" aria-live="polite">
        @if (n()) {
          <span [@roll]="cur()">{{ pad2(cur() + 1) }}</span><i></i><span>{{ pad2(n()) }}</span>
          <b [@roll]="cur()">{{ list()[cur()].name }}</b>
        }
      </div>

      <div class="arrows">
        <button type="button" id="prev" aria-label="Previous piece" (click)="go(-1)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
        <button type="button" id="next" aria-label="Next piece" (click)="go(1)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button>
      </div>
    </section>
  `,
})
export class StageComponent implements OnDestroy {
  shop = inject(ShopService);
  money = money;

  readonly list = computed<Slide[]>(() => {
    const src: Slide[] = this.shop.session() && this.shop.products().length ? this.shop.products() : STATIC_SLIDES;
    return src.filter((p) => CLOTHES[p.image_emoji]);
  });
  readonly n = computed(() => this.list().length);
  private target = signal(0);
  private pos = signal(0);
  readonly cur = computed(() => (this.n() ? this.wrap(this.target(), this.n()) : 0));

  private raf = 0;
  private swipeX: number | null = null;
  private reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  constructor() {
    // when the number of slides changes (signing in swaps the static list for the live one), re-centre
    effect(() => {
      const n = this.n();
      untracked(() => {
        const t = n ? Math.min(this.wrap(this.target(), n), n - 1) : 0;
        this.target.set(t); this.pos.set(t);
      });
    });
  }

  ngOnDestroy() { cancelAnimationFrame(this.raf); }

  // ---- layout
  private wrap = (i: number, n: number) => ((i % n) + n) % n;
  pad2 = (n: number) => String(n).padStart(2, '0');
  key = (s: Slide) => s.image_emoji + ':' + (s.id ?? '');
  img = (s: Slide) => '/static/clothes/' + s.image_emoji + '.webp';
  kind = (s: Slide) => CLOTHES[s.image_emoji];
  blurb = (s: Slide) => BLURB[s.image_emoji] || '';

  dist(i: number): number {
    const n = this.n();
    let d = (((i - this.pos()) % n) + n) % n;
    if (d > n / 2) d -= n;
    return d;
  }
  vis(i: number) { return Math.abs(this.dist(i)) >= 1.001 ? 'hidden' : 'visible'; }

  // ---- movement
  go(dir: number) {
    if (this.n() < 2) return;
    this.target.update((t) => t + dir);
    cancelAnimationFrame(this.raf);
    const from = this.pos(), to = this.target(), t0 = performance.now(), dur = this.reduce ? 0 : 1000;
    const ease = (k: number) => (k < .5 ? 8 * k ** 4 : 1 - (-2 * k + 2) ** 4 / 2);   // easeInOutQuart
    const frame = (t: number) => {
      const k = dur ? Math.min(1, (t - t0) / dur) : 1;
      this.pos.set(from + (to - from) * ease(k));
      if (k < 1) this.raf = requestAnimationFrame(frame);
    };
    this.raf = requestAnimationFrame(frame);
  }

  @HostListener('document:keydown', ['$event'])
  onKey(e: KeyboardEvent) {
    if (/INPUT|TEXTAREA/.test(document.activeElement?.tagName ?? '')) return;
    if (e.key === 'ArrowRight') this.go(1);
    if (e.key === 'ArrowLeft') this.go(-1);
  }
  down(e: PointerEvent) { this.swipeX = (e.target as HTMLElement).closest('button') ? null : e.clientX; }
  up(e: PointerEvent) {
    if (this.swipeX !== null && Math.abs(e.clientX - this.swipeX) > 50) this.go(e.clientX < this.swipeX ? 1 : -1);
    this.swipeX = null;
  }

  // ---- add to cart button
  private product(s: Slide) { return this.shop.session() && s.id != null ? this.shop.productOf(s.id) : undefined; }
  qty(s: Slide) { const p = this.product(s); return p ? this.shop.cart()[p.id] || 0 : 0; }
  disabled(s: Slide) {
    const p = this.product(s);
    if (!this.shop.session() || !p) return false;
    return !!this.shop.hold() || p.available === 0 || this.qty(s) >= this.shop.maxFor(p);
  }
  label(s: Slide) {
    if (!this.shop.session()) return 'Join the drop';
    const p = this.product(s);
    if (!p) return 'Add to cart';
    const qty = this.qty(s), max = this.shop.maxFor(p), out = p.available === 0;
    return this.shop.hold() ? 'Cart reserved' : out ? 'Sold out' : qty >= max ? `${qty} in cart (max)` : qty ? `Add another · ${qty} in cart` : 'Add to cart';
  }
  stock(s: Slide) {
    const p = this.product(s);
    return p ? (p.available === 0 ? 'Sold out' : `${p.available} left`) : '';
  }
  add(s: Slide) {
    if (!this.shop.session()) {
      document.getElementById('auth')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setTimeout(() => document.getElementById('authEmail')?.focus({ preventScroll: true }), 500);
      return;
    }
    if (s.id != null) this.shop.stepCart(s.id, 1);
  }

  goBag() {
    const dest = ['hold', 'cart', 'grid'].map((id) => document.getElementById(id)).find((el) => !!el);
    dest?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}