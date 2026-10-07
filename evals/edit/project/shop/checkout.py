"""Turning a cart into an order."""
from shop.pricing import calc_tax, format_price

DISCOUNTS = {"WELCOME10": 0.10, "HALF": 0.50}
FLAT_DISCOUNTS = {"FIVEOFF": 5.0, "TWENTYOFF": 20.0}


def apply_discount(total, code):
    """The total after a discount code: a percentage or a flat amount off."""
    code = (code or "").strip().upper()
    if code in DISCOUNTS:
        result = total * (1 - DISCOUNTS[code])
    elif code in FLAT_DISCOUNTS:
        result = total - FLAT_DISCOUNTS[code]
    else:
        result = total
    result = round(result, 2)
    return result


def receipt_line(label, amount, region):
    tax = calc_tax(amount, region)
    return f"{label}: {format_price(amount)} (tax {format_price(tax)})"


def checkout(cart, code=None):
    total = apply_discount(cart.total(), code)
    return {"total": total, "text": f"Pay {format_price(total)}"}
