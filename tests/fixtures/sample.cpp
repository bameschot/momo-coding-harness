#include <string>

namespace demo {

class Parser {
public:
    int parse(const std::string &text) { return text.size(); }
};

int run() {
    Parser p;
    return p.parse("hi");
}

}
