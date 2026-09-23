package org.demo;

import java.util.List;

public class Report {
    public String describe(Shape shape) {
        return switch (shape) {
            case Square sq -> "square " + sq.area();
            default -> "shape " + shape.doubled();
        };
    }

    public double viaBase(Shape s) {
        return s.area();
    }

    public double total(List<Square> squares) {
        double t = 0;
        for (Square sq : squares) {
            t += sq.area();
        }
        return t + Square.sum(squares) + Shape.unit().area();
    }
}
