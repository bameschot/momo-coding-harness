#include "box.hpp"

template <typename T>
void Box<T>::put(T v) {
    items_.push_back(v);
}

template <typename T>
T Box<T>::take() {
    T v = items_.back();
    items_.pop_back();
    return v;
}

template <typename T>
Box<T>::~Box() {}

template <typename T>
bool Box<T>::Iter::done() const {
    return true;
}

template <typename T>
Box<T> Box<T>::make() {
    return Box<T>();
}

extern "C" int c_api_version(void) {
    return 2;
}

int use_box() {
    Box<int> b = Box<int>::make();
    b.put(1);
    auto twice = [](int x) { return x * 2; };
    Color c = Color::Red;
    return twice(b.take()) + (c == Color::Green ? 1 : 0);
}
