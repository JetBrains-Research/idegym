"""Entry point for the Python e2e test project."""

from calculator import add, divide, multiply, subtract


def main() -> None:
    print(f"2 + 3 = {add(2, 3)}")
    print(f"10 - 4 = {subtract(10, 4)}")
    print(f"6 * 7 = {multiply(6, 7)}")
    print(f"15 / 3 = {divide(15, 3)}")


if __name__ == "__main__":
    main()
