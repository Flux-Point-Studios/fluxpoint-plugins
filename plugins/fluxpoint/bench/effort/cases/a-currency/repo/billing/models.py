from dataclasses import dataclass


@dataclass(frozen=True)
class Invoice:
    id: str
    customer: str
    cents: int
