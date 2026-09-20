package demo

import kotlin.math.max

class Parser {
    fun parse(text: String): Int = text.length
}

fun run(p: Parser): Int = p.parse("hi")
