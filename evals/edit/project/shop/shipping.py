"""Shipping costs per parcel and zone."""
from dataclasses import dataclass


@dataclass
class Parcel:
    weight: float      # kg


def shipping_cost(parcels, zone):
    """The cost of sending parcels to a zone: a base fee per parcel plus a rate per kg."""
    total = 0.0
    for parcel in parcels:
        if parcel.weight > 0:
            if zone == "domestic":
                total += 2.5 + parcel.weight * 0.5
            elif zone == "eu":
                total += 5.0 + parcel.weight * 1.0
            else:
                total += 12.0 + parcel.weight * 2.0
    return round(total, 2)


def free_shipping(subtotal, zone):
    return zone == "domestic" and subtotal >= 50
