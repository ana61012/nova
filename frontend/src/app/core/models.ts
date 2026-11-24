export interface Product {
  id: number;
  name: string;
  price_cents: number;
  image_emoji: string;
  available: number;
  total: number;
}
export interface Limits { max_qty_per_product: number; max_cart_lines: number; }
export interface QueueState {
  state: 'none' | 'waiting' | 'admitted' | 'expired' | 'done';
  position?: number;
  waiting_total?: number;
  expires_in?: number | null;
}
export interface Line { product_id: number; qty: number; }
export interface Hold { id: number; items: Line[]; expires_at: string; }
export interface Order { id: number; amount_cents: number; }
export interface Paid { items: Line[]; amount_cents: number; }
export interface PaymentsConfig { provider: string; key_id: string | null; }
export interface Session { token: string; email: string; }
export interface Slide { id?: number; name: string; image_emoji: string; price_cents?: number; }

export interface AdminProduct {
  name: string; emoji: string; total: number; available: number; held: number; sold: number; consistent: boolean;
}
export interface AdminStats {
  drop: string | null;
  queue: { waiting: number; admitted: number };
  orders: { total: number; revenue_cents: number; by_status: Record<string, number> };
  outbox_pending: number;
  all_consistent: boolean;
  products: AdminProduct[];
}

export const CLOTHES: Record<string, 'cut' | 'photo'> = { aqua: 'cut', rouge: 'cut', sage: 'photo', pearl: 'photo' };
export const BLURB: Record<string, string> = {
  aqua: 'A beaded halter top with star and crystal charms, with a tiered organza skirt that fades from sea glass to deep water.',
  rouge: 'Wine lace bell sleeves and a ruffled corset front over a cream floral wrap skirt, finished with a fine chain at the waist.',
  sage: 'An embroidered corset with sheer draped sleeves and a layered tulle skirt, scattered with butterflies and pressed flowers.',
  pearl: 'A satin corset dress with ribbon straps, scattered pearls and lace trim, over a full skirt with floating tulle.',
};
export const STATIC_SLIDES: Slide[] = [
  { name: 'Tidepool Set', image_emoji: 'aqua' },
  { name: 'Rosewood Lace Set', image_emoji: 'rouge' },
  { name: 'Fern & Butterfly Set', image_emoji: 'sage' },
  { name: 'Pearl Corset Dress', image_emoji: 'pearl' },
];

export const money = (c: number) => '₹' + (c / 100).toLocaleString('en-IN');
export const mmss = (s: number) => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');