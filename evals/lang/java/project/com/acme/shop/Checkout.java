package com.acme.shop;

import com.acme.shop.Cart.Line;
import java.util.*;

public class Checkout {
    private final Discount discount;

    public Checkout(Discount discount) {
        this.discount = discount;
    }

    public <T extends Cart> Money pay(T cart) {
        Money due = cart.total();
        return discount.applyTwice(due);
    }

    public Status settle(Cart cart) {
        cart.add("fee", 100L);
        pay(cart);
        return Status.PAID;
    }
}
