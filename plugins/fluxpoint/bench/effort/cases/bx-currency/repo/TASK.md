# Invoices carry a currency

Invoices must carry an ISO 4217 currency code instead of assuming USD.

- `Invoice` gains a `currency` field (a 3-letter uppercase code such as
  `"USD"` or `"EUR"`), after `cents`, defaulting to `"USD"` so existing
  callers keep working. Constructing an invoice with a code that is not
  three uppercase ASCII letters raises `ValueError`.
- `codec.dumps` writes the currency. `codec.loads` reads it, and an invoice
  stored before this change (no `currency` key) loads as `"USD"`. A stored
  invoice with an invalid code raises `ValueError`.
- `totals.total_cents(invoices)` keeps returning an int, but raises
  `ValueError` if the invoices are not all in one currency (an empty list
  is 0). Add `totals.total_by_currency(invoices) -> dict[str, int]`.
- `totals.by_customer(invoices)` returns `{(customer, currency): cents}`.
- `cli.main()` prints one line per currency, sorted by code, as
  `<CODE> <cents>`, instead of a single number; with no invoices it prints
  nothing.
- Existing tests must keep passing.
