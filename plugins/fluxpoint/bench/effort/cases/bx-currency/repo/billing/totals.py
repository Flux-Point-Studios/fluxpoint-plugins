def total_cents(invoices):
    return sum(i.cents for i in invoices)


def by_customer(invoices):
    out = {}
    for i in invoices:
        out[i.customer] = out.get(i.customer, 0) + i.cents
    return out
