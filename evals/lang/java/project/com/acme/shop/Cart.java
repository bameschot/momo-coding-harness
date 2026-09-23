package com.acme.shop;

import java.util.ArrayList;
import java.util.List;
import static com.acme.shop.Money.of;

public class Cart {
    public static final int MAX_LINES = 50;

    private final List<Line> lines = new ArrayList<>();

    public static class Line {
        final String sku;
        final Money price;

        Line(String sku, Money price) {
            this.sku = sku;
            this.price = price;
        }
    }

    public class Cursor {
        int pos;

        boolean hasNext() {
            return pos < lines.size();
        }
    }

    public void add(String sku, Money price) {
        lines.add(new Line(sku, price));
    }

    public void add(String sku, long cents) {
        add(sku, of(cents));
    }

    public Money total() {
        return lines.stream().map(l -> l.price).reduce(of(0), Money::add);
    }
}
