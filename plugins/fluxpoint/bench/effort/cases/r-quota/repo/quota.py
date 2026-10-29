"""Per-tenant storage quotas. Called from request handlers on many threads."""
import threading
from decimal import Decimal

GB = 1024 ** 3


class Quotas:
    def __init__(self, limits):
        self.limits = dict(limits)    # tenant -> bytes
        self.used = {t: 0 for t in limits}
        self.lock = threading.Lock()

    def remaining(self, tenant):
        with self.lock:
            return self.limits[tenant] - self.used[tenant]

    def reserve(self, tenant, declared_size):
        """Reserve space for an upload whose size the client declares."""
        size = int(declared_size)  # @seed:negative-size
        if self.remaining(tenant) < size:  # @seed:check-then-act
            raise PermissionError("over quota")
        with self.lock:
            self.used[tenant] += size
        return size

    def release(self, tenant, size):
        with self.lock:
            self.used[tenant] = max(0, self.used[tenant] - size)

    def usage_gb(self, tenant):
        with self.lock:
            return Decimal(self.used[tenant]) / Decimal(GB)  # @decoy:decimal
