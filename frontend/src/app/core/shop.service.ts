import { Injectable, computed, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import { ToastService } from './toast.service';
import { Hold, Limits, Line, Order, Paid, PaymentsConfig, Product, QueueState, Session } from './models';

declare global { interface Window { Razorpay?: any; } }

/**
 * All shop state in one place. Everything lives in memory only: reloading the page
 * (or leaving it) signs you out, exactly like the original page did.
 */
@Injectable({ providedIn: 'root' })
export class ShopService {
  private api = inject(ApiService);
  private toast = inject(ToastService);

  readonly session = signal<Session | null>(null);
  readonly products = signal<Product[]>([]);
  readonly dropName = signal('nova');
  readonly limits = signal<Limits>({ max_qty_per_product: 3, max_cart_lines: 5 });
  readonly queue = signal<QueueState>({ state: 'none' });
  readonly cart = signal<Record<number, number>>({});   // a wish list: holds NO stock until you reserve
  readonly hold = signal<Hold | null>(null);
  readonly order = signal<Order | null>(null);
  readonly paid = signal<Paid | null>(null);
  readonly payments = signal<PaymentsConfig>({ provider: 'simulated', key_id: null });
  readonly now = signal(Date.now());
  readonly busy = signal(false);

  private queueAt = Date.now();
  private stream: EventSource | null = null;
  private pollTimer: ReturnType<typeof setInterval> | undefined;
  private clockTimer: ReturnType<typeof setInterval> | undefined;
  private rzpOpen = false;
  private rzpScript: Promise<void> | null = null;

  // ---------------------------------------------------------------- derived
  readonly cartLines = computed<Line[]>(() => {
    const ps = this.products();
    return Object.entries(this.cart())
      .map(([id, qty]) => ({ product_id: Number(id), qty }))
      .filter((l) => ps.some((p) => p.id === l.product_id));
  });
  readonly cartUnits = computed(() => this.cartLines().reduce((s, l) => s + l.qty, 0));
  readonly cartTotal = computed(() => this.linesTotal(this.cartLines()));
  readonly bagCount = computed(() => {
    const h = this.hold();
    const lines = h ? h.items : Object.entries(this.cart()).map(([, qty]) => ({ qty }));
    return lines.reduce((s, l) => s + l.qty, 0);
  });
  readonly queueLeft = computed(() => {
    const q = this.queue();
    if (q.expires_in == null) return null;
    return Math.max(0, q.expires_in - Math.floor((this.now() - this.queueAt) / 1000));
  });
  readonly holdLeft = computed(() => {
    const h = this.hold();
    if (!h) return 0;
    const iso = /Z|[+-]\d\d:?\d\d$/.test(h.expires_at) ? h.expires_at : h.expires_at + 'Z';
    return Math.max(0, Math.floor((new Date(iso).getTime() - this.now()) / 1000));
  });

  constructor() {
    this.api.onUnauthorized = () => { this.logout(); this.toast.show('You were signed out. Please sign in again.', true); };
    fetch('/api/payments/config').then((r) => r.json()).then((c) => this.payments.set(c)).catch(() => {});
    this.clockTimer = setInterval(() => this.tick(), 500);
  }

  // ---------------------------------------------------------------- helpers
  productOf = (id: number | string) => this.products().find((p) => p.id === Number(id));
  maxFor = (p: Product) => Math.max(0, Math.min(this.limits().max_qty_per_product, p.available));
  linesTotal = (lines: Line[]) =>
    lines.reduce((sum, l) => sum + (this.productOf(l.product_id)?.price_cents || 0) * l.qty, 0);

  // ---------------------------------------------------------------- session
  signedIn(d: { token: string; email: string }) {
    this.session.set({ token: d.token, email: d.email });
    this.api.token = d.token;
    this.cart.set({}); this.hold.set(null); this.order.set(null); this.paid.set(null);
    this.connectStream(); this.load();
    clearInterval(this.pollTimer);
    this.pollTimer = setInterval(() => this.load(), 1500);
  }

  logout() {
    this.session.set(null); this.api.token = null;
    this.hold.set(null); this.order.set(null); this.paid.set(null);
    this.cart.set({}); this.queue.set({ state: 'none' }); this.products.set([]);
    this.stream?.close(); this.stream = null;
    clearInterval(this.pollTimer);
  }

  // ---------------------------------------------------------------- queue (live over SSE)
  private connectStream() {
    this.stream?.close();
    // EventSource cannot send headers, so the token rides in the query string.
    this.stream = new EventSource('/api/queue/stream?token=' + encodeURIComponent(this.session()!.token));
    this.stream.onmessage = (e) => { this.queue.set(JSON.parse(e.data)); this.queueAt = Date.now(); };
  }

  async queueAction(kind: 'join' | 'leave') {
    const r = await this.api.request('/api/queue/' + kind, { method: 'POST' });
    if (r.status === 429) return this.toast.show('Slow down a little.', true);
    if (!r.ok) return this.toast.show('Could not update the queue.', true);
    this.queue.set(await r.json()); this.queueAt = Date.now();
  }

  // ---------------------------------------------------------------- catalog
  async load() {
    try {
      const d = await (await this.api.request('/api/drops/current')).json();
      this.dropName.set(d.name);
      this.products.set(d.products);
      this.limits.set(d.limits);
      this.clampCart();
    } catch { /* transient; the next poll retries */ }
  }

  /** Keep the cart honest as stock changes: drop sold-out lines, trim quantities. */
  private clampCart() {
    if (!this.products().length) return;
    const next = { ...this.cart() };
    let dropped = false, changed = false;
    for (const [id, qty] of Object.entries(next)) {
      const p = this.productOf(id), max = p ? this.maxFor(p) : 0;
      if (max === 0) { delete next[Number(id)]; dropped = changed = true; }
      else if (qty > max) { next[Number(id)] = max; changed = true; }
    }
    if (changed) this.cart.set(next);
    if (dropped) this.toast.show('Some items sold out and were removed from your cart.', true);
  }

  stepCart(id: number | string, dir: number) {
    const p = this.productOf(id);
    if (!p) return;
    const cart = { ...this.cart() };
    const current = cart[p.id] || 0;
    if (dir > 0 && current === 0 && Object.keys(cart).length >= this.limits().max_cart_lines) {
      return this.toast.show(`Up to ${this.limits().max_cart_lines} different items per cart.`, true);
    }
    const next = Math.max(0, Math.min(this.maxFor(p), current + dir));
    if (next === 0) delete cart[p.id]; else cart[p.id] = next;
    this.cart.set(cart);
  }

  removeLine(id: number) { const c = { ...this.cart() }; delete c[id]; this.cart.set(c); }
  clearCart() { this.cart.set({}); }

  // ---------------------------------------------------------------- reserve / checkout / pay
  async reserve() {
    this.busy.set(true);
    const items = Object.entries(this.cart()).map(([id, qty]) => ({ product_id: Number(id), qty }));
    // One key per click: if the network retries this request, the server dedupes it.
    const key = 'cart-' + crypto.randomUUID();
    try {
      const r = await this.api.request('/api/reservations', {
        method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ items }),
      });
      if (r.status === 201 || r.status === 200) {
        const res = await r.json();
        this.cart.set({});
        this.setHold({ id: res.id, items: res.items, expires_at: res.expires_at }, null);
        this.toast.show('Cart reserved. Check out before the timer ends.');
      } else if (r.status === 409) this.toast.show((await this.api.detail(r)) || 'Something in your cart sold out.', true);
      else if (r.status === 403) this.toast.show('Wait for your turn in the queue.', true);
      else if (r.status === 429) this.toast.show('Slow down. Try again in ' + (r.headers.get('Retry-After') || 1) + 's.', true);
      else this.toast.show((await this.api.detail(r)) || 'Something went wrong.', true);
    } catch { this.toast.show('Network error.', true); }
    this.busy.set(false);
    this.load();
  }

  private setHold(h: Hold | null, o: Order | null) {
    this.hold.set(h); this.order.set(o);
    this.tick();   // show the countdown immediately
  }

  async holdAction(action: 'cancel' | 'checkout' | 'rzp' | 'pay' | 'decline') {
    const hold = this.hold();
    if (!hold) return;
    this.busy.set(true);
    try {
      if (action === 'cancel') {
        await this.api.request('/api/reservations/' + hold.id, { method: 'DELETE' });
        this.setHold(null, null); this.toast.show('Hold released.'); this.load(); return;
      }
      if (action === 'checkout') {
        const r = await this.api.request('/api/orders', { method: 'POST', body: JSON.stringify({ reservation_id: hold.id }) });
        if (!r.ok) { this.toast.show((await this.api.detail(r)) || 'Could not start checkout.', true); this.setHold(null, null); return; }
        const o = await r.json();
        this.setHold(hold, { id: o.id, amount_cents: o.amount_cents });
        return;
      }
      if (action === 'rzp') return await this.payWithRazorpay();
      // pay / decline: the dev endpoint plays the payment gateway and sends a signed webhook
      const order = this.order()!;
      const outcome = action === 'pay' ? 'success' : 'fail';
      const r = await this.api.request(`/api/dev/payments/${order.id}/simulate?outcome=${outcome}`, { method: 'POST' });
      const d = await r.json();
      if (d.order_status === 'paid') this.showPaid(hold.items, order.amount_cents);
      else if (outcome === 'fail') this.toast.show('Card declined. Your hold is kept, try paying again.', true);
      else { this.toast.show('Payment did not complete (' + (d.order_status || 'unknown') + ').', true); this.setHold(null, null); this.load(); }
    } finally { this.busy.set(false); }
  }

  dismissPaid() { this.paid.set(null); }

  private showPaid(items: Line[], amountCents: number) {
    this.paid.set({ items, amount_cents: amountCents });
    this.setHold(null, null); this.load();
  }

  // ---- Razorpay Checkout
  private loadRazorpay(): Promise<void> {
    if (window.Razorpay) return Promise.resolve();
    this.rzpScript ??= new Promise<void>((resolve, reject) => {
      const el = document.createElement('script');
      el.src = 'https://checkout.razorpay.com/v1/checkout.js';
      el.onload = () => resolve();
      el.onerror = () => { this.rzpScript = null; reject(new Error('Could not load Razorpay')); };
      document.head.appendChild(el);
    });
    return this.rzpScript;
  }

  private async payWithRazorpay() {
    const items = this.hold()!.items, myOrder = this.order()!;
    try {
      const [r] = await Promise.all([
        this.api.request('/api/payments/razorpay/order', { method: 'POST', body: JSON.stringify({ order_id: myOrder.id }) }),
        this.loadRazorpay(),
      ]);
      if (!r.ok) { this.toast.show((await this.api.detail(r)) || 'Could not start the payment.', true); return; }
      const o = await r.json();
      this.rzpOpen = true;
      const rz = new window.Razorpay({
        key: o.key_id, amount: o.amount, currency: o.currency, name: 'nova',
        description: 'Your nova order', order_id: o.razorpay_order_id,
        prefill: { email: this.session()!.email }, theme: { color: '#3b3340' },
        handler: async (resp: any) => {
          // Razorpay says the payment went through; the server checks its signature before trusting it.
          try {
            const v = await this.api.request('/api/payments/razorpay/verify', { method: 'POST', body: JSON.stringify({
              order_id: myOrder.id, razorpay_order_id: resp.razorpay_order_id,
              razorpay_payment_id: resp.razorpay_payment_id, razorpay_signature: resp.razorpay_signature }) });
            const d = await v.json().catch(() => ({}));
            this.rzpOpen = false;
            if (v.ok && d.order_status === 'paid') return this.showPaid(items, myOrder.amount_cents);
            this.toast.show(v.ok ? 'Payment received but the item sold out, so it will be refunded.' : (d.detail || 'Could not confirm the payment.'), true);
          } catch { this.rzpOpen = false; this.toast.show('Payment taken, but we could not confirm it yet. It will update shortly.', true); }
          this.setHold(null, null); this.load();
        },
        modal: { ondismiss: () => { this.rzpOpen = false; } },
      });
      rz.on('payment.failed', (resp: any) => this.toast.show(resp?.error?.description || 'Payment failed. Try again.', true));
      rz.open();
    } catch { this.toast.show('Could not open Razorpay. Check your connection and try again.', true); }
  }

  // ---------------------------------------------------------------- clock
  private tick() {
    this.now.set(Date.now());
    if (!this.hold()) return;
    if (this.holdLeft() === 0 && this.rzpOpen) return;   // never yank the page out from under an open payment window
    if (this.holdLeft() === 0) { this.setHold(null, null); this.toast.show('Your hold expired.', true); this.load(); }
  }
}