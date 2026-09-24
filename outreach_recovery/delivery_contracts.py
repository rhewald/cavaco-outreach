"""Provider-neutral contracts. Operation IDs are tracking keys, NOT API dedup guarantees.

Implementations must disable automatic mutation retries, use the pinned mailbox /
HubSpot portal, obey timeout_seconds, and return sanitized codes (no raw API errors).
A FOUND result means identity AND payload verified, not merely a search hit.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

class MutationState(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"

class ReconciliationState(str, Enum):
    FOUND = "found"
    NOT_FOUND_YET = "not_found_yet"
    LOOKUP_FAILED = "lookup_failed"

@dataclass(frozen=True)
class MutationResult:
    state: MutationState
    provider_id: Optional[str] = None
    thread_id: Optional[str] = None
    safe_to_retry: bool = False  # Only explicit evidence of non-acceptance.

@dataclass(frozen=True)
class ReconciliationResult:
    state: ReconciliationState
    provider_id: Optional[str] = None
    thread_id: Optional[str] = None

class GmailProvider(Protocol):
    def send(self, payload: dict, *, operation_id: str, timeout_seconds: float) -> MutationResult:
        """Send pinned raw_mime; never generate/rewrite its Message-ID here."""
        ...
    def reconcile(self, payload: dict, *, operation_id: str, timeout_seconds: float) -> ReconciliationResult:
        """Check sent identity/content within payload mailbox; absence is inconclusive."""
        ...

class HubSpotProvider(Protocol):
    def create_email(self, payload: dict, *, operation_id: str, timeout_seconds: float) -> MutationResult: ...
    def reconcile(self, payload: dict, *, operation_id: str, timeout_seconds: float) -> ReconciliationResult:
        """Resolve within pinned portal by verified correlation; unsupported lookup
        remains unresolved. Never create an activity from this method."""
        ...
