package com.acme.shop;

public record Money(long cents, String currency) {
    public static final String DEFAULT_CURRENCY = "EUR";

    public Money add(Money other) {
        return new Money(cents + other.cents, currency);
    }

    public static Money of(long cents) {
        return new Money(cents, DEFAULT_CURRENCY);
    }
}
