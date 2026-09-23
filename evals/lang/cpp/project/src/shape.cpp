#include "geo/shape.hpp"

namespace geo {

std::string Shape::name() const {
    return "shape";
}

Circle::Circle(double r) : r_(r) {}

double Circle::area() const {
    return detail::PI * r_ * r_;
}

double scale(double v, double k) {
    return clamp_to(v * k, 0.0, 1e9);
}

double scale(double v) {
    return scale(v, 2.0);
}

}  // namespace geo
