import { Component, OnDestroy, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { AdminStats, CLOTHES } from '../core/models';
import { NumComponent } from '../shared/num.component';

/**
 * The admin token lives ONLY in this component's memory: never localStorage, sessionStorage
 * or a cookie. Leaving or reloading the page always starts at the login screen. (The server
 * also rejects every token issued before its last restart.)
 */
@Component({
  selector: 'nova-dashboard',
  imports: [FormsModule, RouterLink, NumComponent],
  host: { style: 'display:block' },
  template: `
    @if (!token()) {
      <section id="loginView" class="login">
        <h2>Operator sign-in</h2>
        <div class="sub">Operators only. This is separate from the shop login.</div>
        <form (submit)="login($event)">
          <label for="user">Username</label>
          <input id="user" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required [(ngModel)]="user">
          <label for="pass">Password</label>
          <input id="pass" name="password" type="password" autocomplete="current-password" required [(ngModel)]="pass">
          <button class="buy" type="submit" [disabled]="busy()">Sign in</button>
          <div class="err" role="alert">@if (loginErr()) { <span class="shake">{{ loginErr() }}</span> }</div>
        </form>
        <div class="sub" style="margin-top:8px"><a routerLink="/">Back to shop</a></div>
      </section>
    } @else {
      <div class="dash-wrap swap-in">
        <header class="dash-head">
          <h1><span class="brand">nova</span> live</h1>
          <div class="sub"><span class="live-dot" aria-hidden="true"></span><span>{{ stats()?.drop || '…' }}</span> · refreshes every second · <a routerLink="/">shop</a>
            <button class="out" type="button" (click)="showLogin('You have been logged out.')">Log out</button></div>
        </header>
        <main class="dash">
          <div class="tiles">
            <div class="tile enter" style="--i:0"><div class="k">Waiting in queue</div><div class="v"><nova-num [value]="waiting()" /></div></div>
            <div class="tile enter" style="--i:1"><div class="k">Shopping now</div><div class="v"><nova-num [value]="admitted()" /></div></div>
            <div class="tile enter" style="--i:2"><div class="k">Orders / sec</div><div class="v"><nova-num [value]="ops()" [decimals]="1" /></div></div>
            <div class="tile enter" style="--i:3"><div class="k">Paid orders</div><div class="v"><nova-num [value]="paid()" /></div></div>
            <div class="tile enter" style="--i:4"><div class="k">Revenue</div><div class="v"><nova-num [value]="revenue()" [currency]="true" /></div></div>
            <div class="tile enter" style="--i:5"><div class="k">Outbox backlog</div><div class="v"><nova-num [value]="stats()?.outbox_pending ?? 0" /></div></div>
            <div class="tile enter" style="--i:6"><div class="k">Oversell check</div>
              <div class="v" [class.ok]="stats()?.all_consistent === true" [class.bad]="stats()?.all_consistent === false">{{ stats() ? (stats()!.all_consistent ? 'PASS' : 'FAIL') : '…' }}</div></div>
          </div>

          <table>
            <thead><tr><th>Product</th><th>Stock</th><th>Available</th><th>Held</th><th>Sold</th><th>Audit</th></tr></thead>
            <tbody>
              @for (p of stats()?.products ?? []; track p.name; let i = $index) {
                <tr class="row-in" [style.--i]="i">
                  <td><div class="who">
                    @if (clothes[p.emoji]) { <img class="thumb" [src]="'/static/clothes/' + p.emoji + '.webp'" alt=""> }
                    <span>{{ p.name }}</span></div></td>
                  <td><div class="bar"><i [style.width.%]="(p.sold / p.total) * 100" style="background:var(--ok)"></i><i [style.width.%]="(p.held / p.total) * 100" style="background:var(--warn)"></i></div></td>
                  <td><nova-num [value]="p.available" /> / {{ p.total }}</td>
                  <td><nova-num [value]="p.held" /></td>
                  <td><nova-num [value]="p.sold" /></td>
                  <td [class.ok]="p.consistent" [class.bad]="!p.consistent">{{ p.consistent ? '✓' : '✗' }}</td>
                </tr>
              }
            </tbody>
          </table>
          <div class="legend">Bar: <span class="ok">sold</span> · <span style="color:var(--warn)">held (unpaid)</span> · available (empty). Audit: available + held + sold = total.</div>
        </main>
      </div>
    }
  `,
})
export class DashboardComponent implements OnDestroy {
  clothes = CLOTHES;
  token = signal<string | null>(null);
  stats = signal<AdminStats | null>(null);
  ops = signal(0);
  loginErr = signal('');
  busy = signal(false);
  user = ''; pass = '';

  private prev: { t: number; total: number } | null = null;
  private timer: ReturnType<typeof setInterval> | undefined;

  waiting = () => this.stats()?.queue.waiting ?? 0;
  admitted = () => this.stats()?.queue.admitted ?? 0;
  paid = () => this.stats()?.orders.by_status['paid'] || 0;
  revenue = () => Math.round((this.stats()?.orders.revenue_cents ?? 0) / 100);

  ngOnDestroy() { clearInterval(this.timer); }

  showLogin(message = '') {
    this.token.set(null); this.prev = null; this.stats.set(null); this.ops.set(0);
    clearInterval(this.timer); this.timer = undefined;
    this.pass = '';
    this.loginErr.set('');
    if (message) queueMicrotask(() => this.loginErr.set(message));
    setTimeout(() => document.getElementById(this.user ? 'pass' : 'user')?.focus(), 0);
  }

  private fail(msg: string) { this.loginErr.set(''); queueMicrotask(() => this.loginErr.set(msg)); }

  async login(e: Event) {
    e.preventDefault();
    this.busy.set(true); this.loginErr.set('');
    try {
      const r = await fetch('/api/admin/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: this.user.trim(), password: this.pass }),
      });
      if (r.status === 429) {
        const wait = Number(r.headers.get('Retry-After')) || 5;
        this.fail(`Too many attempts. Wait ${wait}s and try again.`); return;
      }
      if (!r.ok) { this.fail('Invalid ID or password'); return; }
      const token = (await r.json()).token;
      if (!token) { this.fail('Invalid ID or password'); return; }
      this.pass = '';
      this.token.set(token);
      this.tick(); this.timer = setInterval(() => this.tick(), 1000);
    } catch { this.fail('Invalid ID or password'); }
    finally { this.busy.set(false); }
  }

  private async tick() {
    const token = this.token();
    if (!token) return;
    let r: Response;
    try { r = await fetch('/api/stats', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' }); }
    catch { return; }   // server briefly down: keep trying
    if (r.status === 401) { this.showLogin('Session ended (the server restarted or the session expired). Please sign in again.'); return; }
    if (!r.ok || !this.token()) return;
    const s: AdminStats = await r.json();
    const now = performance.now();
    // Orders per second = new orders since the previous poll.
    if (this.prev) this.ops.set(Math.max(0, (s.orders.total - this.prev.total) / ((now - this.prev.t) / 1000)));
    this.prev = { t: now, total: s.orders.total };
    this.stats.set(s);
  }
}