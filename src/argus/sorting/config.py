"""Settings for sorting emails: who Max ASP is, who its partners are, and the thresholds."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from argus.sorting.models import Category

THRESHOLD_ENV = "ARGUS_SORTING_THRESHOLD_{}"


@dataclass(frozen=True)
class Thresholds:
    """The model's confidence must be above these for ARGUS to route on its own (IN-09).

    Moving an email to Other needs more than any other route, because an invoice sent to the
    deletion folder is the most expensive mistake ARGUS can make (ADR 0006). The values are set
    from the shadow run (#72); these defaults are only a careful start.
    """

    incoming_invoice: float = 0.85
    outgoing_invoice: float = 0.85
    payment_reminder: float = 0.85
    other: float = 0.95

    def __post_init__(self) -> None:
        for category in Category:
            value = self.for_category(category)
            if not 0.0 < value < 1.0:
                raise ValueError(f"the threshold for {category} must be between 0 and 1")
        if self.other <= max(self.incoming_invoice, self.outgoing_invoice, self.payment_reminder):
            raise ValueError("the threshold for Other must be higher than every other threshold")

    def for_category(self, category: Category) -> float:
        value: float = getattr(self, category.value)
        return value

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "Thresholds":
        """Read ``ARGUS_SORTING_THRESHOLD_OTHER`` and so on; a value not set keeps its default."""
        values: dict[str, float] = {}
        for category in Category:
            name = THRESHOLD_ENV.format(category.value.upper())
            if raw := environ.get(name, "").strip():
                try:
                    values[category.value] = float(raw)
                except ValueError:
                    raise ValueError(f"{name} must be a number") from None
        return cls(**values)


@dataclass(frozen=True)
class SortingConfig:
    own_domain: str  # Max ASP's email domain
    invoicing_agent: str  # the address the invoicing agent sends from (AR-01)
    own_company: str = "Max ASP GmbH"
    vendors: tuple[str, ...] = ()  # names of the vendors in DATEV
    customers: tuple[str, ...] = ()  # names of the customers in DATEV
    thresholds: Thresholds = field(default_factory=Thresholds)
