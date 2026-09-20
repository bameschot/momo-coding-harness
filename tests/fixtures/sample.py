import os
from collections import OrderedDict


class Parser:
    """A parser."""

    def parse(self, text: str) -> int:
        return len(text)


def run(p: Parser) -> int:
    return p.parse("hi")
