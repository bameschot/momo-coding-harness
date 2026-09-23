package org.demo;

public abstract class Shape {
    public abstract double area();

    public static Shape unit() {
        return new Square(1.0);
    }

    public double doubled() {
        return area() * 2;
    }
}
