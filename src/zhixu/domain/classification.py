"""Data classifications enforced across storage, channels, and model egress."""

from __future__ import annotations

from enum import IntEnum, StrEnum

from .errors import ClassificationNotSupported


class DataClassification(IntEnum):
    """Ordered sensitivity level.

    A higher value must never inherit the more permissive handling of a lower value.
    """

    PUBLIC = 0
    PERSONAL = 1
    CONFIDENTIAL = 2
    SECRET = 3
    PROHIBITED = 4


class SecretKind(StrEnum):
    """Secret handling path; values never contain secret material."""

    MACHINE = "machine"
    HUMAN = "human"


MAX_ORDINARY_CLASSIFICATION = DataClassification.CONFIDENTIAL


def require_ordinary_storage(classification: DataClassification) -> None:
    """Fail closed until the isolated high-sensitivity store is available."""

    if classification > MAX_ORDINARY_CLASSIFICATION:
        raise ClassificationNotSupported(
            f"classification {classification.name} requires the isolated secret store"
        )
