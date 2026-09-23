package org.demo;

import java.util.List;
import java.util.function.Function;

public class Square extends Shape {
    private final double side;

    public Square(double side) {
        this(side, false);
    }

    private Square(double side, boolean checked) {
        super();
        this.side = side;
    }

    @Override
    public double area() {
        return side * side;
    }

    public static <T extends Shape> double sum(List<T> shapes) {
        double s = 0;
        for (T shape : shapes) {
            s += shape.area();
        }
        return s;
    }

    static double blocky(List<Square> xs) {
        if (xs.isEmpty()) {
            double sum = 0;
            return sum;
        }
        return sum(xs);
    }

    public static double biggest(List<Square> squares) {
        Function<Square, Double> areaOf = sq -> sq.area();
        Runnable r = new Runnable() {
            @Override
            public void run() {
                System.out.println("done");
            }
        };
        r.run();
        return squares.stream().map(areaOf).max(Double::compare).orElse(0.0);
    }
}
