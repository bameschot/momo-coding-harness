import { Store, api } from "./store.js";

const { makeCounter } = require("./legacy.js");

const store = Store.create();
store.save("a", 1)?.save("b", 2);
const counter = makeCounter(3);
counter.increment();
console.log(store.size, api.fetchAll(), [...store.keys()]);

(function boot() {
  api.remove(1);
})();
