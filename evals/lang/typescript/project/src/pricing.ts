import { Product, Currency, Rates } from "./types";
import type { PriceMap } from "./types";

export abstract class Pricer {
  abstract price(p: Product): number;

  describe(p: Product): string {
    return `${p.id}: ${this.price(p)}`;
  }
}

export function convert(cents: number): number;
export function convert(cents: number, to: Currency): number;
export function convert(cents: number, to: Currency = Currency.EUR): number {
  return cents * Rates.lookup(to);
}

export class FlatPricer extends Pricer {
  price(p: Product): number {
    return convert(p.cents);
  }
}

export const table: PriceMap = {};
