"""Billing: invoices and refunds for the acme shop. No test covers this module yet."""
from dataclasses import dataclass


@dataclass
class Invoice:
    number: str
    cents: int
    refunded: int = 0

    @property
    def open_cents(self) -> int:
        return self.cents - self.refunded


def refund(invoice: Invoice, cents: int) -> Invoice:
    if cents <= 0 or cents > invoice.open_cents:
        raise ValueError("refund must be positive and at most the open amount")
    invoice.refunded += cents
    return invoice


def total_open(invoices: list[Invoice]) -> int:
    return sum(i.open_cents for i in invoices)
