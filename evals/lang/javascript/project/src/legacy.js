const { formatPrice } = require("./format.js");

function legacyTotal(cart) {
  return formatPrice(cart.total());
}

module.exports = { legacyTotal };
