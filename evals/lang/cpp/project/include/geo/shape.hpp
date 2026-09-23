#pragma once
#include <string>

namespace geo {
namespace detail {
constexpr double PI = 3.14159;
}

class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const = 0;
    std::string name() const;
};

class Circle : public Shape {
public:
    explicit Circle(double r);
    double area() const override;
private:
    double r_;
};

template <typename T>
T clamp_to(T v, T lo, T hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

struct Vec2 {
    double x, y;
    Vec2 operator+(const Vec2& o) const { return {x + o.x, y + o.y}; }
};

double scale(double v, double k);
double scale(double v);
}  // namespace geo
