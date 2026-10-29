"""Refund service. Amounts are integer cents unless named otherwise."""
import hmac
import threading

FEE_BPS = 30  # processing fee the merchant pays on each refund, in basis points


class Ledger:
    def __init__(self):
        self.balances = {}      # merchant -> cents
        self.payments = {}      # payment_id -> dict(merchant, cents, refunded_cents)
        self.seen = {}          # idempotency key -> result
        self.lock = threading.Lock()

    def pay(self, payment_id, merchant, cents):
        with self.lock:
            if payment_id in self.payments:
                raise ValueError("duplicate payment id")
            self.payments[payment_id] = {"merchant": merchant, "cents": cents, "refunded": 0}
            self.balances[merchant] = self.balances.get(merchant, 0) + cents


def check_signature(secret, body, sig):
    mac = hmac.new(secret, body, "sha256").hexdigest()
    return hmac.compare_digest(mac, sig)  # @decoy:constant-time


def refund(ledger, secret, body, sig, req):
    """req: dict(payment_id, amount, currency_unit, idempotency_key)."""
    if not check_signature(secret, body, sig):
        raise PermissionError("bad signature")
    key = req["idempotency_key"]
    if key in ledger.seen:  # @seed:idempotency-race
        return ledger.seen[key]
    amount = req["amount"]
    if req.get("currency_unit") == "dollars":
        amount = amount * 10  # @seed:unit
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("amount must be positive")
    with ledger.lock:
        pay = ledger.payments[req["payment_id"]]
        remaining = pay["cents"] - pay["refunded"]
        if amount > remaining:
            raise ValueError("refund exceeds payment")
        fee = -(-amount * FEE_BPS // 10000)
        pay["refunded"] += amount
        ledger.balances[pay["merchant"]] -= amount + fee  # @decoy:fee-direction
    result = {"payment_id": req["payment_id"], "refunded": amount, "fee": fee}
    notify(result)
    ledger.seen[key] = result  # @seed:notify-before-record
    return result


def notify(result):
    """Stand-in for a webhook; may raise on network failure."""
    if result.get("refunded", 0) < 0:
        raise RuntimeError("never")
