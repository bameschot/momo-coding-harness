package shop

const val MAX_SLUG = 40

val DEFAULT_SEPARATOR = "-"

fun String.slug(): String =
    lowercase().replace(" ", DEFAULT_SEPARATOR).take(MAX_SLUG)

infix fun Int.percentOf(total: Int): Int = total * this / 100
