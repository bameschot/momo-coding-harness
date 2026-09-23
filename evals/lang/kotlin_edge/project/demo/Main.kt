package demo

import demo.Square as Sq

fun main() {
    val sq = Sq(3)
    val items = listOf(sq, Shape("x"))
    println(describe(sq) + biggest(items) + items.second().area())
    println("a b".words.size + Unit.CM.toCm(2.0))
    sq.apply { println(area()) }
}
