import { Injectable } from '@angular/core';

/** Thin fetch wrapper. The bearer token lives in memory only. */
@Injectable({ providedIn: 'root' })
export class ApiService {
  token: string | null = null;
  /** Called when a signed-in request comes back 401 (not for /api/auth). */
  onUnauthorized: (() => void) | null = null;

  async request(path: string, opts: RequestInit = {}): Promise<Response> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...((opts.headers as Record<string, string>) || {}),
    };
    if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
    const r = await fetch(path, { ...opts, headers });
    if (r.status === 401 && this.token && !path.startsWith('/api/auth')) {
      this.onUnauthorized?.();
      throw new Error('signed out');
    }
    return r;
  }

  /** FastAPI sends a string for our own errors and a list for validation errors. */
  async detail(r: Response): Promise<string> {
    try {
      const d = (await r.json()).detail;
      if (typeof d === 'string') return d;
      if (r.status === 422) return 'Please check your details and try again.';
      return 'That request was not valid.';
    } catch {
      return '';
    }
  }
}