package app

import shop.Checkout
import shop.Product
import shop.slug

fun main() {
    val items = listOf(Product("Tea Pot", 1200), Product.free("Bag"))
    println(Checkout.receipt(items))
    println("Summer Sale".slug())
}
