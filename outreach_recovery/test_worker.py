"""Fault-injection tests. No credentials, network, PostgreSQL, or real sends."""
import unittest
from dataclasses import dataclass
from typing import Optional

from outreach_recovery.worker import Intent, Lookup, LookupState, Worker


class WorkerCrash(BaseException):
    """Simulates abrupt process death, bypassing normal exception handlers."""


@dataclass
class Row:
    intent: Intent
    state: str = "pending"
    token: Optional[str] = None
    lease_until: int = 0
    due: int = 0
    started: bool = False
    approved: bool = True
    snapshot: int = 1


class MemoryRepository:
    """Contract fake retained across worker restarts; not a PostgreSQL adapter."""
    def __init__(self):
        self.row = Row(Intent("op-1", "mailbox-1", "<op-1@example.test>",
                              b"Message-ID: <op-1@example.test>\r\n\r\nHello"))
        self.now = 0
        self.sequence = 0
        self.version = 1
        self.logs = {}
        self.completions = 0
        self.fail_commit = False
        self.crash_after_marker = False

    def claim(self, intent_id, recovery):
        row = self.row
        assert intent_id == row.intent.id
        if row.due > self.now:
            return None
        if recovery:
            eligible = row.state == "uncertain" or (
                row.state == "processing" and row.lease_until <= self.now)
        else:
            eligible = (row.state == "pending" and not row.started
                        and row.approved and row.snapshot == self.version)
        if not eligible:
            return None
        self.sequence += 1
        row.token = str(self.sequence)
        row.state = "processing"
        row.lease_until = self.now + 120
        return row.token

    def read(self, intent_id):
        return self.row.intent

    def owned(self, token):
        return (self.row.state == "processing" and self.row.token == token
                and self.row.lease_until > self.now)

    def begin_dispatch(self, intent_id, token):
        row = self.row
        if not self.owned(token):
            return False
        if row.started or not row.approved or row.snapshot != self.version:
            return False
        row.started = True
        if self.crash_after_marker:
            raise WorkerCrash()
        return True

    def complete(self, intent_id, token, provider_id):
        if not self.owned(token):
            return False
        if self.fail_commit:
            raise RuntimeError("Database commit unavailable")
        self.row.state = "completed"
        self.row.token = None
        self.row.lease_until = 0
        self.version += 1
        self.completions += 1
        self.logs.setdefault("hubspot:" + intent_id, provider_id)
        return True

    def uncertain(self, intent_id, token, reason):
        if self.owned(token):
            self.row.state = "uncertain"
            self.row.token = None
            self.row.lease_until = 0
            self.row.due = self.now + 600


class FakeGmail:
    def __init__(self):
        self.sent = []
        self.visible = True
        self.lookup_error = False
        self.after_accept = None
        self.on_lookup = None
        self.lookups = 0

    def lookup(self, intent):
        self.lookups += 1
        if self.on_lookup:
            self.on_lookup()
        if self.lookup_error:
            raise ConnectionError("Search unavailable")
        if self.sent and self.visible:
            return Lookup(LookupState.FOUND, "gmail-1")
        return Lookup(LookupState.NOT_FOUND)

    def send(self, intent):
        self.sent.append(intent)
        if self.after_accept:
            self.after_accept()
        return "gmail-1"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryRepository()
        self.gmail = FakeGmail()
        self.worker = Worker(self.db, self.gmail)

    def restart(self):
        self.db.now += 601
        self.worker = Worker(self.db, self.gmail)

    def crash(self):
        raise WorkerCrash()

    def timeout(self):
        raise TimeoutError("Response lost after Gmail accepted message")

    def test_crash_after_accept_recovers_without_resend(self):
        self.gmail.after_accept = self.crash
        with self.assertRaises(WorkerCrash):
            self.worker.dispatch("op-1")
        self.restart()
        self.worker.dispatch("op-1")
        self.worker.reconcile("op-1")
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.db.row.state, "completed")
        self.assertEqual(self.db.logs, {"hubspot:op-1": "gmail-1"})

    def test_delayed_visibility_never_reopens_dispatch(self):
        self.gmail.after_accept = self.timeout
        self.gmail.visible = False
        self.worker.dispatch("op-1")
        for _ in range(3):
            self.restart()
            self.worker.reconcile("op-1")
            self.worker.dispatch("op-1")
            self.assertEqual(self.db.row.state, "uncertain")
            self.assertEqual(len(self.gmail.sent), 1)
        self.gmail.visible = True
        self.restart()
        self.worker.reconcile("op-1")
        self.assertEqual(self.db.row.state, "completed")

    def test_commit_failure_recovers_after_accepted_send(self):
        self.db.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.worker.dispatch("op-1")
        self.assertEqual(self.db.logs, {})
        self.db.fail_commit = False
        self.restart()
        self.worker.reconcile("op-1")
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.db.completions, 1)

    def test_crash_before_network_requires_review_not_resend(self):
        self.db.crash_after_marker = True
        with self.assertRaises(WorkerCrash):
            self.worker.dispatch("op-1")
        self.restart()
        self.worker.reconcile("op-1")
        self.worker.dispatch("op-1")
        self.assertEqual(self.db.row.state, "uncertain")
        self.assertEqual(self.gmail.sent, [])

    def test_search_failure_blocks_send_and_honors_backoff(self):
        self.gmail.lookup_error = True
        self.worker.dispatch("op-1")
        self.worker.reconcile("op-1")
        self.assertEqual(self.gmail.lookups, 1)
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(self.db.row.state, "uncertain")

    def test_stale_completion_does_not_create_log_or_increment_version(self):
        token = self.db.claim("op-1", recovery=False)
        self.db.begin_dispatch("op-1", token)
        self.restart()
        new_token = self.db.claim("op-1", recovery=True)
        self.assertFalse(self.db.complete("op-1", token, "gmail-1"))
        self.assertEqual(self.db.version, 1)
        self.assertEqual(self.db.logs, {})
        self.assertTrue(self.db.complete("op-1", new_token, "gmail-1"))
        self.assertFalse(self.db.complete("op-1", new_token, "gmail-1"))
        self.assertEqual(self.db.version, 2)

    def test_active_lease_excludes_another_worker(self):
        token = self.db.claim("op-1", recovery=False)
        self.worker.dispatch("op-1")
        self.worker.reconcile("op-1")
        self.assertEqual(self.db.row.token, token)
        self.assertEqual(self.gmail.sent, [])

    def test_expired_preflight_cannot_start_send(self):
        self.gmail.on_lookup = lambda: setattr(self.db, "now", 121)
        self.worker.dispatch("op-1")
        self.assertEqual(self.gmail.sent, [])
        self.assertFalse(self.db.row.started)

    def test_new_reply_during_preflight_invalidates_draft(self):
        self.gmail.on_lookup = lambda: setattr(self.db, "version", 2)
        self.worker.dispatch("op-1")
        self.assertEqual(self.gmail.sent, [])
        self.assertFalse(self.db.row.started)

    def test_unapproved_draft_cannot_send(self):
        self.db.row.approved = False
        self.worker.dispatch("op-1")
        self.assertEqual(self.gmail.sent, [])

    def test_unprocessed_hubspot_log_does_not_resend_email(self):
        self.worker.dispatch("op-1")
        for _ in range(3):
            self.restart()
            self.worker.dispatch("op-1")
            self.worker.reconcile("op-1")
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(len(self.db.logs), 1)
        self.assertEqual(self.db.completions, 1)


if __name__ == "__main__":
    unittest.main()
