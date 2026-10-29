import re
from dataclasses import dataclass

CODE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Invoice:
    id: str
    customer: str
    cents: int
    currency: str = "USD"

    def __post_init__(self):
        if not isinstance(self.currency, str) or not CODE.match(self.currency):
            raise ValueError(f"bad currency {self.currency!r}")
