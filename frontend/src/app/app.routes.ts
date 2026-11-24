import { Routes } from '@angular/router';
import { ShopComponent } from './shop/shop.component';
import { DashboardComponent } from './dashboard/dashboard.component';

export const routes: Routes = [
  { path: '', component: ShopComponent, title: 'nova', data: { animation: 'shop' } },
  { path: 'dashboard', component: DashboardComponent, title: 'nova · live', data: { animation: 'dashboard' } },
  { path: '**', redirectTo: '' },
];