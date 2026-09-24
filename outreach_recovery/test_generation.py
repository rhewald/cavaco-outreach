import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

from outreach_recovery.generation import (GenerationRepository, GenerationWorker,
    ProcessGenerator, PermanentModelError, build_prompt)
try:
    import psycopg2
except ImportError:
    psycopg2 = None

DSN = os.environ.get("OUTREACH_TEST_DATABASE_URL")


# Trusted test-only subprocess adapters, never network calls.
def example_model(messages, timeout_seconds):
    return "Thank you for your reply."


def slow_model(messages, timeout_seconds):
    time.sleep(10)
    return "Too late"


def invalid_model(messages, timeout_seconds):
    return "<END_OF_TURN>"


class ProcessDeadlineTests(unittest.TestCase):
    def test_model_deadline_is_enforced(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            ProcessGenerator(__name__ + ":slow_model", timeout_seconds=0.2).generate([])
        self.assertLess(time.monotonic()-started, 5)

    def test_model_returns_body(self):
        self.assertEqual(ProcessGenerator(__name__ + ":example_model", 5).generate([]), "Thank you for your reply.")

    def test_internal_markers_are_rejected(self):
        with self.assertRaises(PermanentModelError):
            ProcessGenerator(__name__ + ":invalid_model", 5).generate([])


class CallbackGenerator:
    timeout_seconds = 5

    def __init__(self, callback):
        self.callback = callback

    def generate(self, messages):
        return self.callback(messages)


class TargetRepository(GenerationRepository):
    """Focus worker integration tests on their own fixtures, not other test jobs."""
    def __init__(self, dsn, job_id):
        super().__init__(dsn)
        self.job_id = job_id

    def claim(self, lease_seconds=120, job_id=None):
        return super().claim(lease_seconds, job_id or self.job_id)


@unittest.skipUnless(DSN and psycopg2, "Requires disposable database and psycopg2")
class GenerationDatabaseTests(unittest.TestCase):
    def sql(self, statement, params=(), fetch=True):
        conn = psycopg2.connect(DSN, options="-c statement_timeout=5000 -c lock_timeout=1000")
        try:
            with conn, conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall() if fetch else None
        finally:
            conn.close()

    def setUp(self):
        self.mailbox = str(uuid4())
        self.conversation = str(uuid4())
        self.sql("INSERT INTO outreach_pilot.mailboxes(id,email) VALUES (%s,%s)",
                 (self.mailbox, self.mailbox+"@test.example"), False)
        self.sql("INSERT INTO outreach_pilot.conversations(id,mailbox_id,gmail_thread_id) VALUES (%s,%s,'generation')",
                 (self.conversation, self.mailbox), False)
        self.seller = self.sql("INSERT INTO outreach_pilot.seller_profiles(facts,approved_by) VALUES ('{\"company\":\"Cavaco\",\"policy\":\"Do not invent prices\"}', 'test-reviewer') RETURNING id")[0][0]
        self.sql("INSERT INTO outreach_pilot.conversation_contexts(conversation_id,seller_profile_id,prospect_research) VALUES (%s,%s,'Untrusted research')",
                 (self.conversation, self.seller), False)
        self.message = self.ingest("first")[1]
        self.job_id = self.sql("SELECT id FROM outreach_pilot.reply_jobs WHERE triggering_message_id=%s", (self.message,))[0][0]
        self.repo = TargetRepository(DSN, self.job_id)

    def ingest(self, message):
        return self.sql("SELECT * FROM outreach_pilot.ingest_reply(%s,%s,'generation',NULL,ARRAY[]::text[],'Interested',%s)",
                        (self.mailbox, message, datetime.now(timezone.utc)))[0]

    def expire(self):
        self.sql("UPDATE outreach_pilot.reply_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (self.job_id,), False)

    def state(self):
        return self.sql("SELECT state,attempts FROM outreach_pilot.reply_jobs WHERE id=%s", (self.job_id,))[0]

    def test_competing_workers_claim_only_once(self):
        barrier = Barrier(8)
        def claim(_):
            barrier.wait(timeout=10)
            return self.repo.claim()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(claim, range(8)))
        self.assertEqual(sum(j is not None for j in claims), 1)
        self.assertEqual(self.state(), ("processing", 1))

    def test_expired_owner_cannot_publish_after_reclaim(self):
        old = self.repo.claim()
        self.expire()
        current = self.repo.claim()
        self.assertNotEqual(old["lease_token"], current["lease_token"])
        self.assertIsNone(self.repo.save(old, "Stale"))
        self.assertFalse(self.repo.fail(old, "Stale failure"))
        draft = self.repo.save(current, "Current")
        self.assertIsNotNone(draft)
        self.assertEqual(self.repo.save(current, "Duplicate completion"), draft)
        self.assertEqual(self.sql("SELECT body FROM outreach_pilot.drafts WHERE id=%s", (draft,)), [("Current",)])

    def test_new_reply_during_generation_discards_old_result(self):
        def model(_):
            self.ingest("newer")
            return "Stale output"
        self.assertEqual(GenerationWorker(self.repo, CallbackGenerator(model)).run_once(), "discarded")
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.drafts WHERE conversation_id=%s", (self.conversation,))[0][0], 0)
        latest = self.sql("SELECT id FROM outreach_pilot.reply_jobs WHERE conversation_id=%s AND state='pending'", (self.conversation,))[0][0]
        worker = GenerationWorker(TargetRepository(DSN, latest), CallbackGenerator(lambda _: "Current reply"))
        self.assertEqual(worker.run_once(), "drafted")
        self.assertEqual(self.sql("SELECT state,version_snapshot FROM outreach_pilot.drafts WHERE conversation_id=%s", (self.conversation,)), [("pending_review", 2)])

    def test_no_database_lock_is_held_during_model_call(self):
        def model(_):
            self.sql("SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE NOWAIT", (self.conversation,))
            self.sql("SELECT id FROM outreach_pilot.reply_jobs WHERE id=%s FOR UPDATE NOWAIT", (self.job_id,))
            return "Review this"
        self.assertEqual(GenerationWorker(self.repo, CallbackGenerator(model)).run_once(), "drafted")

    def test_retry_backoff_and_snapshot_stability(self):
        first = self.repo.claim()
        prompt = build_prompt(first["context_snapshot"])
        self.assertEqual(first["context_snapshot"]["history"], [])
        self.assertEqual(prompt[0]["role"], "system")
        self.repo.fail(first, "Transient error")
        self.assertIsNone(self.repo.claim())
        self.sql("INSERT INTO outreach_pilot.conversation_contexts(conversation_id,seller_profile_id,prospect_research) VALUES (%s,%s,'Changed research')",
                 (self.conversation, self.seller), False)
        self.sql("UPDATE outreach_pilot.reply_jobs SET next_attempt_at=clock_timestamp() WHERE id=%s", (self.job_id,), False)
        second = self.repo.claim()
        self.assertEqual(first["context_snapshot"], second["context_snapshot"])
        self.assertEqual(second["attempts"], 2)

    def test_exhausted_attempts_enter_dead_letter(self):
        self.sql("UPDATE outreach_pilot.reply_jobs SET max_attempts=1 WHERE id=%s", (self.job_id,), False)
        job = self.repo.claim()
        self.repo.fail(job, "Failed")
        self.assertEqual(self.state(), ("dead_letter", 1))
        self.assertIsNone(self.repo.claim())

    def test_crash_on_final_attempt_enters_dead_letter(self):
        self.sql("UPDATE outreach_pilot.reply_jobs SET max_attempts=1 WHERE id=%s", (self.job_id,), False)
        self.repo.claim()
        self.expire()
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.state(), ("dead_letter", 1))

    def test_permanent_model_failure_is_not_retried(self):
        def model(_):
            raise PermanentModelError("Invalid seller configuration")
        self.assertEqual(GenerationWorker(self.repo, CallbackGenerator(model)).run_once(), "dead_letter")
        self.assertEqual(self.state(), ("dead_letter", 1))

    def test_timeout_schedules_retry(self):
        def model(_):
            raise TimeoutError()
        self.assertEqual(GenerationWorker(self.repo, CallbackGenerator(model)).run_once(), "retry")
        self.assertEqual(self.state(), ("pending", 1))
        self.assertIsNone(self.repo.claim())

    def test_expired_lease_without_reclaim_cannot_save(self):
        job = self.repo.claim()
        self.expire()
        self.assertIsNone(self.repo.save(job, "Late output"))

    def test_snapshot_and_ledger_are_immutable(self):
        self.repo.claim()
        with self.assertRaises(psycopg2.errors.RaiseException):
            self.sql("UPDATE outreach_pilot.reply_jobs SET context_snapshot='{}' WHERE id=%s", (self.job_id,), False)
        with self.assertRaises(psycopg2.errors.RaiseException):
            self.sql("UPDATE outreach_pilot.messages SET body_text='Changed' WHERE id=%s", (self.message,), False)

    def test_missing_seller_configuration_routes_to_review(self):
        conversation = self.sql("INSERT INTO outreach_pilot.conversations(mailbox_id,gmail_thread_id) VALUES (%s,'unconfigured') RETURNING id", (self.mailbox,))[0][0]
        self.sql("SELECT * FROM outreach_pilot.ingest_reply(%s,'unconfigured','unconfigured',NULL,ARRAY[]::text[],'Hello',now())", (self.mailbox,))
        job_id = self.sql("SELECT id FROM outreach_pilot.reply_jobs WHERE conversation_id=%s", (conversation,))[0][0]
        self.assertIsNone(GenerationRepository(DSN).claim(job_id=job_id))
        self.assertEqual(self.sql("SELECT state,attempts FROM outreach_pilot.reply_jobs WHERE id=%s", (job_id,)), [("dead_letter", 0)])

    def test_stale_failure_cannot_requeue_superseded_job(self):
        old = self.repo.claim()
        self.ingest("new-reply")
        self.assertFalse(self.repo.fail(old, "Old failure"))
        self.assertEqual(self.state()[0], "superseded")

    def test_future_messages_do_not_enter_saved_snapshot(self):
        first = self.repo.claim()
        self.ingest("future")
        stored = self.sql("SELECT context_snapshot FROM outreach_pilot.reply_jobs WHERE id=%s", (self.job_id,))[0][0]
        self.assertEqual(stored, first["context_snapshot"])
        self.assertEqual(stored["target_conversation_version"], 1)


if __name__ == "__main__":
    unittest.main()
