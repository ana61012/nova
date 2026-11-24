import { Component, OnDestroy, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../core/api.service';
import { ShopService } from '../core/shop.service';

type Mode = 'signup' | 'login' | 'code';
const MODE: Record<Mode, { btn: string; hint: string; pw: string }> = {
  signup: { btn: 'Create account', hint: "Pick a password. You'll log in on the next step.", pw: 'new-password' },
  login:  { btn: 'Log in', hint: '', pw: 'current-password' },
  code:   { btn: 'Send me a code', hint: "No password needed. We'll email you a 6-digit code.", pw: '' },
};

@Component({
  selector: 'nova-auth',
  imports: [FormsModule],
  host: { style: 'display:block' },
  template: `
    <section id="welcome" class="welcome">
      <div>
        <h1 class="rise">Dresses with a little moonlight in them.</h1>
        <p class="lede rise">Small runs. A fair queue. Never oversold.</p>
        <form class="auth rise" id="auth" (submit)="submit($event)">
          <h2>Join the drop</h2>
          @if (step() === 'email') {
            <div class="row" style="flex-wrap:nowrap">
              <button type="button" [class]="mode() === 'signup' ? 'buy' : 'ghost'" style="flex:1" (click)="setMode('signup')">Create account</button>
              <button type="button" [class]="mode() !== 'signup' ? 'buy' : 'ghost'" style="flex:1" (click)="setMode('login')">Log in</button>
            </div>
            <!-- step 1: email + password (or just email, for the emailed-code option) -->
            <div class="swap-in">
              <input id="authEmail" name="email" type="email" placeholder="Email" autocomplete="email" style="width:100%" [(ngModel)]="email">
              @if (mode() !== 'code') {
                <input id="authPassword" name="password" type="password" placeholder="Password (at least 8 characters)"
                       [attr.autocomplete]="MODE[mode()].pw" style="width:100%;margin-top:10px" [(ngModel)]="password">
              }
              <div class="sub" style="margin:8px 0 14px">{{ hint() }}</div>
              <button class="buy" type="submit" style="width:100%" [disabled]="sending()">{{ MODE[mode()].btn }}</button>
              <div style="margin-top:12px;text-align:center">
                <button class="link" type="button" (click)="setMode(mode() === 'code' ? 'login' : 'code')">
                  {{ mode() === 'code' ? 'Use a password instead' : 'Email me a sign-in code instead' }}</button>
              </div>
            </div>
          } @else {
            <!-- step 2: code -->
            <div class="swap-in">
              <div class="sub" style="margin-bottom:10px">Enter the code we sent to <b>{{ pendingEmail }}</b>.</div>
              <input id="authCode" name="code" inputmode="numeric" pattern="\\d{6}" maxlength="6" placeholder="123456"
                     autocomplete="one-time-code" style="letter-spacing:6px;text-align:center;font-size:20px" [(ngModel)]="code">
              <button class="buy" type="submit" style="width:100%;margin-top:12px">Verify and continue</button>
              <div class="row" style="margin-top:10px">
                <button class="ghost" type="button" style="flex:1" [disabled]="resendLeft() > 0" (click)="sendCode()">
                  {{ resendLeft() > 0 ? 'Resend in ' + resendLeft() + 's' : 'Resend code' }}</button>
                <button class="ghost" type="button" style="flex:1" (click)="changeEmail()">Change email</button>
              </div>
            </div>
          }
          <div class="err">@if (err()) { <span class="shake">{{ err() }}</span> }</div>
        </form>
      </div>
    </section>
  `,
})
export class AuthComponent implements OnDestroy {
  private api = inject(ApiService);
  private shop = inject(ShopService);
  MODE = MODE;

  mode = signal<Mode>('signup');
  step = signal<'email' | 'code'>('email');
  err = signal('');
  hint = signal(MODE.signup.hint);
  sending = signal(false);
  resendLeft = signal(0);
  email = ''; password = ''; code = '';
  pendingEmail: string | null = null;
  private resendTimer: ReturnType<typeof setInterval> | undefined;

  ngOnDestroy() { clearInterval(this.resendTimer); }

  setMode(m: Mode) {
    this.mode.set(m); this.hint.set(MODE[m].hint); this.err.set('');
  }

  private authStep(step: 'email' | 'code') {
    this.step.set(step); this.err.set('');
    if (step === 'code') {
      this.code = '';
      setTimeout(() => document.getElementById('authCode')?.focus(), 0);
    }
  }

  private startResendCountdown(seconds: number) {
    clearInterval(this.resendTimer);
    let left = seconds;
    this.resendLeft.set(left);
    this.resendTimer = setInterval(() => {
      left -= 1;
      this.resendLeft.set(Math.max(0, left));
      if (left <= 0) clearInterval(this.resendTimer);
    }, 1000);
  }

  async sendCode() {
    this.err.set('');
    const r = await this.api.request('/api/auth/request-code', { method: 'POST', body: JSON.stringify({ email: this.pendingEmail }) });
    if (r.status === 429) {
      const wait = Number(r.headers.get('Retry-After')) || 60;
      // Already have a live code? Let them type it while the resend timer runs.
      this.authStep('code'); this.startResendCountdown(wait);
      this.err.set((await this.api.detail(r)) || 'Please wait a moment.');
      return;
    }
    if (!r.ok) { this.err.set((await this.api.detail(r)) || 'Could not send a code.'); return; }
    const d = await r.json();
    this.authStep('code'); this.startResendCountdown(d.resend_after);
  }

  changeEmail() { clearInterval(this.resendTimer); this.pendingEmail = null; this.authStep('email'); }

  private signedIn(d: { token: string; email: string }) {
    clearInterval(this.resendTimer); this.pendingEmail = null; this.password = '';
    this.shop.signedIn(d);
  }

  async submit(e: Event) {
    e.preventDefault();
    if (this.step() === 'email') {
      const email = this.email.trim().toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { this.fail('Enter a valid email address.'); return; }
      if (this.mode() === 'code') { this.pendingEmail = email; return this.sendCode(); }

      const password = this.password;
      if (this.mode() === 'signup' && password.length < 8) { this.fail('Use a password of at least 8 characters.'); return; }
      if (!password) { this.fail('Enter your password.'); return; }
      this.sending.set(true); this.err.set('');
      try {
        const r = await this.api.request('/api/auth/' + this.mode(), { method: 'POST', body: JSON.stringify({ email, password }) });
        if (r.status === 429) { this.fail('Too many attempts. Wait a moment and try again.'); return; }
        if (!r.ok) {
          const msg = await this.api.detail(r);
          this.fail(msg || (this.mode() === 'signup' ? 'Could not create the account.' : 'Could not log in.'));
          return;
        }
        if (this.mode() === 'signup') {
          // Account created: don't sign in automatically. Send them to the login form.
          this.setMode('login');
          this.email = email; this.password = '';
          this.hint.set('Account created. Please log in to continue.');
          setTimeout(() => document.getElementById('authPassword')?.focus(), 0);
          return;
        }
        this.signedIn(await r.json());
      } catch { this.fail('Could not reach the server.'); }
      finally { this.sending.set(false); }
      return;
    }
    this.err.set('');
    const r = await this.api.request('/api/auth/verify-code', {
      method: 'POST', body: JSON.stringify({ email: this.pendingEmail, code: this.code.trim() }),
    });
    if (!r.ok) { this.fail((await this.api.detail(r)) || 'Could not sign in.'); return; }
    this.signedIn(await r.json());
  }

  /** Clear then set, so the shake replays even when the same message repeats. */
  private fail(msg: string) { this.err.set(''); queueMicrotask(() => this.err.set(msg)); }
}