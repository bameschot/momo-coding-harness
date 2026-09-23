import React from "react";
import { useCart, CartLine } from "./useCart";

export interface CartViewProps {
  title: string;
  initial?: CartLine[];
}

const LineItem = ({ line }: { line: CartLine }) => (
  <li className="line">{line.sku} × {line.qty}</li>
);

export default function CartView({ title, initial }: CartViewProps) {
  const { lines, add } = useCart(initial);
  return (
    <section>
      <h2>{title}</h2>
      <ul>{lines.map((l) => <LineItem key={l.sku} line={l} />)}</ul>
      <button onClick={() => add("tea")}>Add tea</button>
    </section>
  );
}
