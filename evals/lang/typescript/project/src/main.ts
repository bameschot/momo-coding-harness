import { FlatPricer, convert } from "./index";
import type { Product } from "./types";

const tea: Product = { id: "tea", cents: 1200 };

export function run(): string {
  return new FlatPricer().describe(tea) + convert(5);
}
