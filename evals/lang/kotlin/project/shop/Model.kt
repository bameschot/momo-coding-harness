package shop

data class Product(val name: String, val cents: Int) {
    fun label(): String = name.slug()

    companion object {
        const val FREE = 0

        fun free(name: String) = Product(name, FREE)
    }
}

sealed class Payment {
    data class Card(val last4: String) : Payment()
    object Cash : Payment()
}

interface Pricer {
    fun price(p: Product): Int
}
