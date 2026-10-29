def total_by_currency(invoices):
    out = {}
    for i in invoices:
        out[i.currency] = out.get(i.currency, 0) + i.cents
    return out


def total_cents(invoices):
    by = total_by_currency(invoices)
    if len(by) > 1:
        raise ValueError("invoices in more than one currency")
    return sum(by.values())


def by_customer(invoices):
    out = {}
    for i in invoices:
        k = (i.customer, i.currency)
        out[k] = out.get(k, 0) + i.cents
    return out
