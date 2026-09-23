#pragma once
#include <vector>

template <typename T>
class Box {
public:
    void put(T v);
    T take();
    ~Box();

    class Iter {
    public:
        bool done() const;
    };

    static Box<T> make();

private:
    std::vector<T> items_;
};

enum class Color { Red, Green };

extern "C" {
int c_api_version(void);
}
