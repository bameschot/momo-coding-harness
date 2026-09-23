from .models import *
from .models import Order, Rush, summary


def main() -> None:
    first: Order = Order.empty()
    rush = Rush(5)
    print(summary(first), summary(rush), scale(3))
    handlers = list(map(summary, [first, rush]))
    print(handlers)


if __name__ == "__main__":
    main()
