import shop.cart as cartmod
from shop.cart import Cart, Item
from shop.pricing import fetch_rates


def checkout(cart: Cart) -> float:
    rates = fetch_rates("eu")
    cart.add(Item("fee", 1.0))
    return cart.total()


def reset(cart: cartmod.Cart) -> None:
    cart.clear()
