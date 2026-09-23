package com.acme.shop;

public enum Status {
    OPEN, PAID, SHIPPED;

    public boolean isFinal() {
        return this == SHIPPED;
    }
}
