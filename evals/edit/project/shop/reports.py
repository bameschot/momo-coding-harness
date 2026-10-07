"""Reports over orders and the tax ledger."""
from shop.pricing import calc_tax, format_price


class Ledger:
    """Tax already booked, entry by entry."""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def calc_tax(self):
        """Total tax booked in this ledger (not the pricing function of the same name)."""
        return round(sum(tax for _, tax in self.entries), 2)


def tax_report(amounts, region, ledger):
    owed = round(sum(calc_tax(a, region) for a in amounts), 2)
    booked = ledger.calc_tax()
    return {"owed": owed, "booked": booked, "open": round(owed - booked, 2)}


def revenue_line(amounts):
    return "Revenue: " + format_price(sum(amounts))
