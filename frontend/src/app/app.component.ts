import { Component, HostListener, inject } from '@angular/core';
import { ChildrenOutletContexts, RouterOutlet } from '@angular/router';
import { routeFade } from './shared/animations';
import { ToastComponent } from './shared/toast.component';

@Component({
  selector: 'nova-root',
  imports: [RouterOutlet, ToastComponent],
  animations: [routeFade],
  template: `
    <div [@routeFade]="routeKey()"><router-outlet /></div>
    <nova-toast />
  `,
})
export class AppComponent {
  private contexts = inject(ChildrenOutletContexts);

  routeKey(): string {
    return this.contexts.getContext('primary')?.route?.snapshot?.data?.['animation'] ?? '';
  }

  /** Back/forward can restore a page from the browser cache with its JS state intact;
   *  treat that like a fresh visit (sign-ins live in memory only). */
  @HostListener('window:pageshow', ['$event'])
  onPageShow(e: PageTransitionEvent) {
    if (e.persisted) location.reload();
  }
}