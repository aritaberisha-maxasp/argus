"""Settings for sorting emails: who Max ASP is, who its partners are, and the thresholds."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from argus.sorting.models import Category

THRESHOLD_ENV = "ARGUS_SORTING_THRESHOLD_{}"
# Max ASP's legal name as it stands on invoices. The default of SortingConfig.own_company; code that
# has the config passes that value instead.
OWN_COMPANY = "Max ASP GmbH"


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
    own_company: str = OWN_COMPANY
    # The email domains the vendors and customers in DATEV really send from, exactly as written
    # in their From addresses. A domain that only looks like one of these never matches (#70).
    vendor_domains: frozenset[str] = frozenset()
    customer_domains: frozenset[str] = frozenset()
    thresholds: Thresholds = field(default_factory=Thresholds)

    def __post_init__(self) -> None:
        # An empty setting would make an email with no readable From address look like it
        # claims to be the invoicing agent or someone at Max ASP.
        if "@" not in self.invoicing_agent.strip():
            raise ValueError("the invoicing agent must be a full email address")
        if not self.own_domain.strip():
            raise ValueError("Max ASP's own email domain must be set")
