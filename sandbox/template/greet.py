"""Greet people by name."""

import sys


def greet(name: str) -> str:
    return f"Hello, {name}!"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: greet.py NAME", file=sys.stderr)
        return 2
    print(greet(argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
