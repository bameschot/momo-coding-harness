import { Cart } from "./index.js";

const cart = new Cart();
cart.add({ sku: "tea", cents: 1200 });

async function lazyReport() {
  const { legacyTotal } = await import("./legacy.js");
  return legacyTotal(cart);
}

lazyReport().then(console.log);
