"""Sorting each email in the accounts mailbox into one category (ADR 0006, IN-04, IN-09)."""

from argus.sorting.config import SortingConfig, Thresholds
from argus.sorting.language_model import (
    AzureOpenAIModel,
    LanguageModel,
    ModelAnswerInvalid,
    ModelUnavailable,
    RecordedModel,
)
from argus.sorting.models import (
    Attachment,
    Category,
    EmailToSort,
    ModelAnswer,
    Route,
    SortingDecision,
)
from argus.sorting.router import sort_email

__all__ = [
    "Attachment",
    "AzureOpenAIModel",
    "Category",
    "EmailToSort",
    "LanguageModel",
    "ModelAnswer",
    "ModelAnswerInvalid",
    "ModelUnavailable",
    "RecordedModel",
    "Route",
    "SortingConfig",
    "SortingDecision",
    "Thresholds",
    "sort_email",
]
