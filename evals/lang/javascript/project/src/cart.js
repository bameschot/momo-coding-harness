import formatList, { formatPrice } from "./format.js";

export class Cart {
  items = [];
  static MAX = 20;

  add = (item) => {
    this.items.push(item);
  };

  total() {
    return this.items.reduce((s, i) => s + i.cents, 0);
  }

  describe() {
    return formatList(this.items) + " = " + formatPrice(this.total());
  }
}
