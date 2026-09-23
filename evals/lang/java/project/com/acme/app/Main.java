package com.acme.app;

import com.acme.shop.*;

public class Main {
    public static void main(String[] args) {
        Cart cart = new Cart();
        cart.add("tea", 1200L);
        System.out.println(cart.total());
    }
}
