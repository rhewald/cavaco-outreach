"""Validation boundaries and legacy upgrades against real PostgreSQL."""
import copy
import json
import unittest
from pathlib import Path
try:
    from psycopg2.extras import Json
except ImportError:
    Json = None
from outreach_recovery.generation import GenerationRepository, InvalidContext
import test_generation as fixtures
from test_generation import DSN, psycopg2, CallbackGenerator
from outreach_recovery.generation import GenerationWorker

# Reuse setup/helpers without inheriting and rerunning the generation suite.
@unittest.skipUnless(DSN and psycopg2, "Requires disposable database")
class SellerValidationTests(unittest.TestCase):
    sql = fixtures.GenerationDatabaseTests.sql
    setUp = fixtures.GenerationDatabaseTests.setUp
    ingest = fixtures.GenerationDatabaseTests.ingest
    state = fixtures.GenerationDatabaseTests.state

    def facts(self):
        return json.loads(Path(__file__).with_name("seller_profile.example.json").read_text())

    def test_required_fields_and_types(self):
        base = self.facts()
        for field in base:
            for value in (None, False, [], {}):
                if field == "allowed_claims" and value == []:
                    continue
                facts = copy.deepcopy(base)
                facts[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(InvalidContext):
                    self.repo.create_seller_profile(facts, "reviewer")
            facts = copy.deepcopy(base)
            del facts[field]
            with self.subTest(missing=field), self.assertRaises(InvalidContext):
                self.repo.create_seller_profile(facts, "reviewer")

    def test_errors_name_fields_without_echoing_values(self):
        facts = self.facts()
        facts["company_name"] = "  \t\n"
        with self.assertRaisesRegex(InvalidContext, "company_name: expected nonblank"):
            self.repo.create_seller_profile(facts, "reviewer")
        with self.assertRaisesRegex(InvalidContext, "approved_by"):
            self.repo.create_seller_profile(self.facts(), "\n\t")
        facts = self.facts()
        facts["secret-extra"] = "sensitive-value"
        with self.assertRaises(InvalidContext) as raised:
            self.repo.create_seller_profile(facts, "reviewer")
        self.assertNotIn("sensitive-value", str(raised.exception))

    def test_pricing_modes_and_fixed_plans(self):
        facts = self.facts()
        for mode in ("undisclosed", "custom"):
            facts["pricing"] = {"mode": mode}
            self.assertIsNotNone(self.repo.create_seller_profile(facts, "reviewer"))
        plan = {"name": "Pilot", "amount_minor": 15000, "currency": "EUR", "billing_interval": "month"}
        facts["pricing"] = {"mode": "fixed", "plans": [plan]}
        self.assertIsNotNone(self.repo.create_seller_profile(facts, "reviewer"))
        for field, value in (("amount_minor", -1), ("amount_minor", True), ("amount_minor", 1.5),
                             ("amount_minor", "15000"), ("amount_minor", 100000000001),
                             ("currency", "XYZ"), ("billing_interval", "weekly"), ("name", " ")):
            bad = copy.deepcopy(facts)
            bad["pricing"]["plans"][0][field] = value
            with self.subTest(field=field,value=value), self.assertRaisesRegex(InvalidContext, field):
                self.repo.create_seller_profile(bad, "reviewer")
        for pricing in ({"mode":"fixed","plans":[]}, {"mode":"custom","amount_minor":500},
                        {"mode":"fixed","plans":[plan,plan]}, {"mode":"fixed","plans":[None]}):
            facts["pricing"] = pricing
            with self.subTest(pricing=pricing), self.assertRaises(InvalidContext):
                self.repo.create_seller_profile(facts,"reviewer")

    def test_direct_sql_cannot_bypass_validation(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            self.sql("INSERT INTO outreach_pilot.seller_profiles(facts,approved_by) VALUES (%s,'reviewer')",
                     (Json({"company":"legacy"}),), False)
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.seller_profiles WHERE facts=%s",
                                  (Json({"company":"legacy"}),)), [(0,)])

    def legacy_profile(self):
        # Only this disposable test cluster uses privileged trigger disabling to
        # reproduce rows written before migration 004. Restore before committing.
        with self.repo.connection() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE outreach_pilot.seller_profiles DISABLE TRIGGER validate_seller_profile")
            cur.execute("INSERT INTO outreach_pilot.seller_profiles(facts,approved_by) VALUES ('{\"company\":\"old\"}','legacy') RETURNING id")
            seller = cur.fetchone()[0]
            cur.execute("ALTER TABLE outreach_pilot.seller_profiles ENABLE TRIGGER validate_seller_profile")
        return seller

    def test_legacy_profile_cannot_be_linked_to_new_context(self):
        seller = self.legacy_profile()
        with self.assertRaises(psycopg2.errors.CheckViolation):
            self.sql("INSERT INTO outreach_pilot.conversation_contexts(conversation_id,seller_profile_id) VALUES (%s,%s)",
                     (self.conversation,seller),False)

    def test_legacy_pinned_snapshot_dead_letters_without_model_call(self):
        self.sql("UPDATE outreach_pilot.reply_jobs SET context_snapshot=%s WHERE id=%s",
                 (Json({"seller_facts":{"company":"old"}}), self.job_id), False)
        def forbidden(_):
            self.fail("Invalid seller facts reached model")
        self.assertEqual(GenerationWorker(self.repo, CallbackGenerator(forbidden)).run_once(), "idle")
        self.assertEqual(self.state(), ("dead_letter",0))
        error = self.sql("SELECT last_error FROM outreach_pilot.reply_jobs WHERE id=%s",(self.job_id,))[0][0]
        self.assertIn("schema_version",error)

    def test_invalid_insert_rolls_back_context_work(self):
        before=self.sql("SELECT count(*) FROM outreach_pilot.seller_profiles")[0][0]
        with self.assertRaises(psycopg2.errors.CheckViolation):
            with self.repo.connection() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO outreach_pilot.seller_profiles(facts,approved_by) VALUES (%s,'reviewer')", (Json(self.facts()),))
                cur.execute("INSERT INTO outreach_pilot.seller_profiles(facts,approved_by) VALUES ('{}','reviewer')")
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.seller_profiles")[0][0],before)
