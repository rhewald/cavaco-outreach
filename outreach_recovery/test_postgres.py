"""Opt-in integration checks: use ONLY a disposable database with inbound.sql applied."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

try:
    import psycopg2
except ImportError:
    psycopg2 = None

DSN = os.environ.get("OUTREACH_TEST_DATABASE_URL")


@unittest.skipUnless(DSN and psycopg2, "Requires disposable OUTREACH_TEST_DATABASE_URL and psycopg2")
class PostgresIngestionTests(unittest.TestCase):
    def connect(self):
        conn = psycopg2.connect(DSN, options="-c statement_timeout=15000 -c lock_timeout=5000")
        self.addCleanup(conn.close)
        return conn

    def setUp(self):
        self.mailbox = str(uuid4())
        self.conversation = str(uuid4())
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO outreach_pilot.mailboxes(id,email) VALUES (%s,%s)",
                        (self.mailbox, self.mailbox + "@example.test"))
            cur.execute("INSERT INTO outreach_pilot.conversations(id,mailbox_id,gmail_thread_id) VALUES (%s,%s,'thread')",
                        (self.conversation, self.mailbox))

    def ingest(self, message, thread="thread", refs=None, conn=None, barrier=None):
        # Each concurrent invocation owns its own connection/transaction.
        owned = conn is None
        if owned:
            conn = psycopg2.connect(DSN, options="-c statement_timeout=15000 -c lock_timeout=5000")
        try:
            if barrier:
                barrier.wait(timeout=10)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM outreach_pilot.ingest_reply(%s,%s,%s,%s,%s,%s,%s)",
                            (self.mailbox, message, thread, None, refs or [],
                             "Same body is legitimate in distinct messages", datetime.now(timezone.utc)))
                row = cur.fetchone()
            if owned:
                conn.commit()
            return row
        finally:
            if owned:
                conn.close()

    def version(self):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT version_counter FROM outreach_pilot.conversations WHERE id=%s", (self.conversation,))
            return cur.fetchone()[0]

    def test_concurrent_duplicate_delivery(self):
        barrier = Barrier(12)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.ingest("duplicate", barrier=barrier), range(12)))
        self.assertEqual(sum(r[0] == "ingested" for r in results), 1)
        self.assertEqual(sum(r[0] == "duplicate" for r in results), 11)
        self.assertEqual(len({r[1] for r in results}), 1)
        self.assertEqual(self.version(), 1)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM outreach_pilot.reply_jobs WHERE conversation_id=%s", (self.conversation,))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_concurrent_distinct_messages_have_unique_versions(self):
        barrier = Barrier(12)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda n: self.ingest("message-" + str(n), barrier=barrier), range(12)))
        self.assertEqual(sorted(r[2] for r in results), list(range(1, 13)))
        self.assertEqual(self.version(), 12)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT version_snapshot FROM outreach_pilot.reply_jobs WHERE conversation_id=%s AND state='pending'",
                        (self.conversation,))
            self.assertEqual(cur.fetchall(), [(12,)])

    def test_unrelated_conversation_is_not_blocked(self):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO outreach_pilot.conversations(mailbox_id,gmail_thread_id) VALUES (%s,'other')", (self.mailbox,))
        locker = self.connect()
        with locker.cursor() as cur:
            cur.execute("SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE", (self.conversation,))
        try:
            self.assertEqual(self.ingest("other-message", thread="other")[0], "ingested")
        finally:
            locker.rollback()

    def test_same_conversation_lock_rolls_back_and_can_retry(self):
        locker = self.connect()
        with locker.cursor() as cur:
            cur.execute("SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE", (self.conversation,))
        waiter = self.connect()
        try:
            with waiter.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='150ms'")
            with self.assertRaises(psycopg2.errors.LockNotAvailable):
                self.ingest("locked", conn=waiter)
            waiter.rollback()
        finally:
            locker.rollback()
        self.assertEqual(self.version(), 0)
        self.assertEqual(self.ingest("locked")[2], 1)

    def test_job_insert_failure_rolls_back_everything(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""CREATE FUNCTION outreach_pilot.test_fail_job() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'Injected enqueue failure'; END; $$;
                    CREATE TRIGGER test_fail_job BEFORE INSERT ON outreach_pilot.reply_jobs
                    FOR EACH ROW EXECUTE FUNCTION outreach_pilot.test_fail_job();
                    SAVEPOINT before_ingestion;""")
            with self.assertRaises(psycopg2.errors.RaiseException):
                self.ingest("rollback", conn=conn)
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT before_ingestion")
                cur.execute("SELECT version_counter FROM outreach_pilot.conversations WHERE id=%s", (self.conversation,))
                self.assertEqual(cur.fetchone()[0], 0)
                for table in ("messages", "inbound_receipts"):
                    cur.execute("SELECT count(*) FROM outreach_pilot." + table + " WHERE mailbox_id=%s", (self.mailbox,))
                    self.assertEqual(cur.fetchone()[0], 0)
                cur.execute("SELECT count(*) FROM outreach_pilot.reply_jobs WHERE conversation_id=%s", (self.conversation,))
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.rollback()  # Also removes the fault-injection trigger/function.
        self.assertEqual(self.ingest("rollback")[2], 1)

    def test_header_resolution_and_thread_fallback(self):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO outreach_pilot.messages(mailbox_id,conversation_id,gmail_message_id,
                mime_message_id,direction,body_text,received_at,conversation_version)
                VALUES (%s,%s,'sent','<sent@example.test>','outbound','Hi',now(),1)""",
                        (self.mailbox, self.conversation))
            cur.execute("UPDATE outreach_pilot.conversations SET version_counter=1 WHERE id=%s", (self.conversation,))
        self.assertEqual(self.ingest("header-only", thread=None, refs=["<sent@example.test>"])[2], 2)
        self.assertEqual(self.ingest("fallback", refs=["<unknown@example.test>"])[2], 3)

    def test_existing_ledger_entry_without_receipt_is_duplicate(self):
        self.ingest("existing")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM outreach_pilot.inbound_receipts WHERE mailbox_id=%s", (self.mailbox,))
        self.assertEqual(self.ingest("existing")[0], "duplicate")
        self.assertEqual(self.version(), 1)

    def test_message_identity_is_scoped_to_mailbox(self):
        first = self.ingest("same-provider-id")
        self.setUp()  # Another independently authenticated mailbox/conversation.
        second = self.ingest("same-provider-id")
        self.assertEqual(second[0], "ingested")
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(second[2], 1)

    def test_sql_regression_cases(self):
        conn = self.connect()
        conn.autocommit = True
        script = (Path(__file__).parent / "inbound_checks.sql").read_text()
        script = "\n".join(line for line in script.splitlines() if not line.startswith("\\"))
        with conn.cursor() as cur:
            cur.execute(script)


if __name__ == "__main__":
    unittest.main()
