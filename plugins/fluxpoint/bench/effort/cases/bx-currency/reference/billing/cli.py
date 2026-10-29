import sys
from billing import store, totals


def main():
    for code, cents in sorted(totals.total_by_currency(store.load_all()).items()):
        print(code, cents)


if __name__ == "__main__":
    sys.exit(main())
