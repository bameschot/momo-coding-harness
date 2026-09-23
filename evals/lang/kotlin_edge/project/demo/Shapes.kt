package demo

typealias Area = Double

enum class Unit(val factor: Double) {
    CM(1.0), INCH(2.54);

    fun toCm(v: Double): Double = v * factor
}

val String.words: List<String>
    get() = split(" ")

fun <T> List<T>.second(): T = this[1]

open class Shape(val name: String) {
    open fun area(): Area = 0.0

    init {
        require(name.isNotEmpty())
    }
}

class Square(val side: Double) : Shape("square") {
    constructor(side: Int) : this(side.toDouble())

    override fun area(): Area = side * side

    inner class Scaler {
        fun scaled(k: Double) = area() * k
    }
}

fun describe(s: Shape): String = when (s) {
    is Square -> "square ${s.area()}"
    else -> "shape"
}

fun biggest(shapes: List<Shape>): Double {
    val (first, second) = shapes.take(2)
    return shapes.maxOf { it.area() } + first.area() + second.area()
}
