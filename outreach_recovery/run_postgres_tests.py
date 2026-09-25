"""Run against a disposable local PostgreSQL instance; no live credentials needed.

Requires pgserver==0.1.4 and psycopg2-binary==2.9.12 in a test environment.
Run from repository root: python -m outreach_recovery.run_postgres_tests
"""
import os
import tempfile
import unittest
from pathlib import Path

import pgserver
import psycopg2


def main():
    root = Path(__file__).resolve().parent
    # pgserver stops and deletes this isolated cluster when its handle exits.
    server = pgserver.get_server(Path(tempfile.mkdtemp(prefix="outreach-pg-")), cleanup_mode="delete")
    uri = server.get_uri()
    conn = psycopg2.connect(uri)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            print("PostgreSQL:", cur.fetchone()[0], flush=True)
            cur.execute((root / "inbound.sql").read_text())
            cur.execute((root / "002_ingestion_concurrency.sql").read_text())
            cur.execute((root / "003_draft_generation.sql").read_text())
            cur.execute((root / "004_seller_validation.sql").read_text())
            cur.execute((root / "005_human_review.sql").read_text())
            cur.execute((root / "006_delivery_outbox.sql").read_text())
            cur.execute((root / "007_initial_outreach.sql").read_text())
            cur.execute((root / "008_provider_observations.sql").read_text())
            cur.execute((root / "009_crm_activity_jobs.sql").read_text())
            cur.execute((root / "010_crm_handoff.sql").read_text())
            cur.execute((root / "011_gmail_monitor.sql").read_text())
            cur.execute((root / "012_review_context.sql").read_text())
            cur.execute((root / "013_review_contact_links.sql").read_text())
            cur.execute((root / "014_delivery_simulations.sql").read_text())
    finally:
        conn.close()
    os.environ["OUTREACH_TEST_DATABASE_URL"] = uri
    suite = unittest.defaultTestLoader.discover(str(root), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
