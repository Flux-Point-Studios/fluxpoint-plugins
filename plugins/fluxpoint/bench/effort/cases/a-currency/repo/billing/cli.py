import sys
from billing import store, totals


def main():
    print(totals.total_cents(store.load_all()))


if __name__ == "__main__":
    sys.exit(main())
