import { animate, keyframes, query, style, transition, trigger } from '@angular/animations';

const EASE = 'cubic-bezier(.22, .8, .24, 1)';
/** Fire on a real value change only, never on the first render. */
const changed = (from: string, to: string) => from !== 'void' && from !== to;

/** Page to page: the new page rises in while the old one steps aside. */
export const routeFade = trigger('routeFade', [
  transition('* <=> *', [
    query(':leave', [style({ display: 'none' })], { optional: true }),
    query(':enter', [
      style({ opacity: 0, transform: 'translateY(26px)' }),
      animate(`750ms 60ms ${EASE}`, style({ opacity: 1, transform: 'none' })),
    ], { optional: true }),
  ]),
]);

/** Panels (cart, hold, receipt): unfold and fade in, fold away on leave. */
export const panel = trigger('panel', [
  transition(':enter', [
    style({ opacity: 0, height: 0, marginTop: 0, paddingTop: 0, paddingBottom: 0, transform: 'translateY(14px) scale(.98)', overflow: 'hidden' }),
    animate(`600ms ${EASE}`, style({ opacity: 1, height: '*', marginTop: '*', paddingTop: '*', paddingBottom: '*', transform: 'none' })),
  ]),
  transition(':leave', [
    style({ overflow: 'hidden' }),
    animate(`380ms ${EASE}`, style({ opacity: 0, height: 0, marginTop: 0, paddingTop: 0, paddingBottom: 0, transform: 'translateY(-8px) scale(.98)' })),
  ]),
]);

/** A row in a list (cart line, hold line): slides in, collapses on leave. */
export const lineItem = trigger('lineItem', [
  transition(':enter', [
    style({ opacity: 0, height: 0, transform: 'translateX(-18px)', overflow: 'hidden' }),
    animate(`420ms ${EASE}`, style({ opacity: 1, height: '*', transform: 'none' })),
  ]),
  transition(':leave', [
    style({ overflow: 'hidden' }),
    animate(`300ms ${EASE}`, style({ opacity: 0, height: 0, transform: 'translateX(24px)' })),
  ]),
]);

/** Pop when a bound number changes (bag badge, stepper quantity, add button). */
export const bump = trigger('bump', [
  transition(changed, [
    animate('420ms', keyframes([
      style({ transform: 'scale(1)', offset: 0 }),
      style({ transform: 'scale(1.3)', offset: .35 }),
      style({ transform: 'scale(.94)', offset: .7 }),
      style({ transform: 'scale(1)', offset: 1 }),
    ])),
  ]),
]);

/** Number roll: the new value slides up into place. */
export const roll = trigger('roll', [
  transition(changed, [
    animate(`480ms ${EASE}`, keyframes([
      style({ opacity: 0, transform: 'translateY(60%)', offset: 0 }),
      style({ opacity: 1, transform: 'none', offset: 1 }),
    ])),
  ]),
]);

/** Toast rises from the bottom and sinks back. */
export const toastAnim = trigger('toastAnim', [
  transition(':enter', [
    style({ opacity: 0, transform: 'translate(-50%, 24px) scale(.96)' }),
    animate(`420ms ${EASE}`, style({ opacity: 1, transform: 'translate(-50%, 0) scale(1)' })),
  ]),
  transition(':leave', [animate('260ms ease-in', style({ opacity: 0, transform: 'translate(-50%, 12px)' }))]),
]);