export class Store {
  #items = new Map();
  static instances = 0;

  get size() {
    return this.#items.size;
  }

  set limit(n) {
    this.max = n;
  }

  #touch(key) {
    return key.trim();
  }

  save(key, value) {
    this.#items.set(this.#touch(key), value);
    return this;
  }

  static create() {
    Store.instances += 1;
    return new Store();
  }

  *keys() {
    yield* this.#items.keys();
  }
}

export const api = {
  fetchAll() {
    return [];
  },
  remove: (id) => id,
};

export const Base = class {
  hello() {
    return "hi";
  }
};
