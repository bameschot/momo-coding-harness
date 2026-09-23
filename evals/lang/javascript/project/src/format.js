export const CURRENCY = "EUR";

export function formatPrice(cents) {
  return `${(cents / 100).toFixed(2)} ${CURRENCY}`;
}

export default function formatList(items) {
  return items.map((i) => formatPrice(i.cents)).join(", ");
}
