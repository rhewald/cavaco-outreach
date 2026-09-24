"""Delivery policy; repository and Gmail adapters must implement the contracts below."""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol


class LookupState(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True)
class Intent:
    id: str
    mailbox_id: str
    mime_message_id: str
    raw_mime: bytes
    thread_id: Optional[str] = None


@dataclass(frozen=True)
class Lookup:
    state: LookupState
    provider_id: Optional[str] = None


class Repository(Protocol):
    def claim(self, intent_id: str, recovery: bool) -> Optional[str]:
        """Atomically lease eligible, due work with a fresh token.

        Dispatch: only pending, never-started intents with current approval,
        conversation version, and outreach eligibility. Recovery: uncertain or
        expired processing intents. Neither lane claims an active lease.
        """
        ...

    def read(self, intent_id: str) -> Intent: ...

    def begin_dispatch(self, intent_id: str, token: str) -> bool:
        """Commit a permanent dispatch-started marker before the API call.

        Compare active lease token and expiry, revalidate eligibility atomically,
        and refuse if previously started. Freeze identity/content permanently.
        """
        ...

    def complete(self, intent_id: str, token: str, provider_id: str) -> bool:
        """One transaction: CAS active lease, record success, mark draft sent,
        append message/update version once, enqueue unique HubSpot log intent.
        Repeated or stale completion must have no effects.
        """
        ...

    def uncertain(self, intent_id: str, token: str, reason: str) -> None:
        """CAS active lease, clear lease, schedule reconciliation with backoff.
        Never translate an ambiguous outcome into send-retry eligibility.
        """
        ...


class Gmail(Protocol):
    def lookup(self, intent: Intent) -> Lookup:
        """Use intent mailbox; validate sent message identity and content.
        A missing search result is not proof of non-delivery.
        """
        ...

    def send(self, intent: Intent) -> str:
        """Send immutable MIME with optional threadId; no automatic send retries."""
        ...


class Worker:
    def __init__(self, repository: Repository, gmail: Gmail):
        self.repository = repository
        self.gmail = gmail

    def dispatch(self, intent_id: str) -> None:
        token = self.repository.claim(intent_id, recovery=False)
        if token is None:
            return
        intent = self.repository.read(intent_id)
        result = self._lookup(intent)
        if result.state is LookupState.FOUND:
            self.repository.complete(intent_id, token, result.provider_id)
            return
        if result.state is LookupState.FAILED:
            self.repository.uncertain(intent_id, token, "Preflight lookup failed")
            return
        if not self.repository.begin_dispatch(intent_id, token):
            return
        try:
            provider_id = self.gmail.send(intent)
        except Exception:
            self.repository.uncertain(intent_id, token, "Send outcome unknown")
            return
        if not provider_id:
            self.repository.uncertain(intent_id, token, "Missing provider result")
            return
        # Deliberately outside the send exception handler: database failure must
        # leave a recoverable in-flight intent, never cause another send.
        self.repository.complete(intent_id, token, provider_id)

    def reconcile(self, intent_id: str) -> None:
        token = self.repository.claim(intent_id, recovery=True)
        if token is None:
            return
        result = self._lookup(self.repository.read(intent_id))
        if result.state is LookupState.FOUND:
            self.repository.complete(intent_id, token, result.provider_id)
        else:
            self.repository.uncertain(intent_id, token, result.state.value)

    def _lookup(self, intent: Intent) -> Lookup:
        try:
            result = self.gmail.lookup(intent)
            if result.state is LookupState.FOUND and not result.provider_id:
                return Lookup(LookupState.FAILED)
            return result
        except Exception:
            return Lookup(LookupState.FAILED)
