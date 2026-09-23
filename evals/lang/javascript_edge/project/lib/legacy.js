function Counter(start) {
  this.value = start;
}

Counter.prototype.increment = function () {
  this.value += 1;
  return this.value;
};

exports.makeCounter = function (n) {
  return new Counter(n);
};

module.exports.resetAll = (counters) => counters.forEach((c) => (c.value = 0));
