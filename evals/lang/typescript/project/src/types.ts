export interface Product {
  id: string;
  cents: number;
}

export type PriceMap = Record<string, number>;

export enum Currency {
  EUR = "EUR",
  USD = "USD",
}

export namespace Rates {
  export const DEFAULT = 1;
  export function lookup(c: Currency): number {
    return c === Currency.EUR ? DEFAULT : 1.1;
  }
}
