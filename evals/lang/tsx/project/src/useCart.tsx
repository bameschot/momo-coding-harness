import { useState } from "react";

export interface CartLine {
  sku: string;
  qty: number;
}

export function useCart(initial: CartLine[] = []) {
  const [lines, setLines] = useState(initial);
  const add = (sku: string) => setLines([...lines, { sku, qty: 1 }]);
  return { lines, add };
}
