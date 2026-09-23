#include <iostream>
#include "geo/shape.hpp"

using geo::Circle;

static double total_area(const geo::Shape& s, int n) {
    return s.area() * n;
}

int main() {
    Circle c(2.0);
    geo::Vec2 v = geo::Vec2{1, 2} + geo::Vec2{3, 4};
    std::cout << total_area(c, 3) << geo::scale(v.x) << std::endl;
    return 0;
}
