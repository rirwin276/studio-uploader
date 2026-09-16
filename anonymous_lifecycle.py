"""Shared marker for anonymous preview deletion versus first claim.

The collection's existing compare-and-set claim metafield is the single
arbitration point across replicas. This value is never a customer owner.
"""
DELETION_CLAIM = "__anonymous_preview_deleting__"


def is_deletion_claim(value: str) -> bool:
    return str(value or "").strip() == DELETION_CLAIM
