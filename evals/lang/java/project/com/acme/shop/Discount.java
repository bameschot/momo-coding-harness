package com.acme.shop;

public interface Discount {
    int PERCENT_CAP = 90;

    Money apply(Money price);

    default Money applyTwice(Money price) {
        return apply(apply(price));
    }
}
