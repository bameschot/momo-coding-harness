package shop

import shop.Product as Item
import kotlin.math.max

object Checkout {
    fun total(items: List<Item>, discountPct: Int): Int {
        val sum = items.sumOf { it.cents }
        return max(0, sum - (discountPct percentOf sum))
    }

    fun receipt(items: List<Item>): String =
        items.joinToString { it.label() } + " = " + total(items, 10)
}
