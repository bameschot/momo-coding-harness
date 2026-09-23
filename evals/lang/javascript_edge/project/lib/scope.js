export function total(xs) {
  return xs.length;
}

export function report(xs) {
  if (xs.length) {
    const total = 1;
    console.log(total);
  }
  return total(xs);
}

export function legacy(xs) {
  var tally = 0;
  if (xs) {
    var total = 2;
    tally = total;
  }
  return tally;
}
