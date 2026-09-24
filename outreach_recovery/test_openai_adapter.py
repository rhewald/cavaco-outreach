import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

import httpx
import openai
from outreach_recovery import openai_adapter as adapter
from outreach_recovery.generation import (GenerationWorker, ProcessGenerator,
    ModelConfigurationError, PermanentModelError)
import test_generation as fixtures

ENV = {"OPENAI_API_KEY":"test-only-secret", "OUTREACH_OPENAI_MODEL":"test-model"}
PROMPT = [{"role":"system","content":"Approved facts"}, {"role":"user","content":"Private prospect reply"}]


def response_body():
    return {"id":"resp_test", "object":"response", "created_at":0, "model":"test-model",
            "status":"completed", "output":[{"id":"msg_test", "type":"message", "role":"assistant",
            "status":"completed", "content":[{"type":"output_text", "text":"Thanks for your interest.", "annotations":[]}]}],
            "usage":{"input_tokens":12,"output_tokens":6,"total_tokens":18}}


def subprocess_model(messages, timeout_seconds):
    # Exercise the real SDK in the spawned process, with an in-memory transport.
    def factory(key, timeout):
        return openai.OpenAI(api_key=key,max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200,json=response_body()))))
    adapter._client = factory
    return adapter.generate(messages,timeout_seconds=timeout_seconds)


subprocess_model.preflight = adapter.preflight


def subprocess_config_failure(messages, timeout_seconds):
    raise ModelConfigurationError("provider_access_denied")


class OpenAIAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ,ENV,clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []

    def invoke(self, status=200, payload=None, error=None):
        def transport(request):
            self.calls.append(request)
            if error:
                raise error(request=request)
            return httpx.Response(status, json=response_body() if payload is None else payload,
                                  headers={"x-request-id":"req_test"})
        def factory(key,timeout):
            return openai.OpenAI(api_key=key, max_retries=0, timeout=timeout,
                http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        with patch.object(adapter,"_client",factory):
            return adapter.generate(PROMPT,timeout_seconds=5)

    def test_request_and_telemetry_do_not_include_side_effects_or_private_text(self):
        with self.assertLogs(adapter.LOG,level="INFO") as logs:
            self.assertEqual(self.invoke(),"Thanks for your interest.")
        body = json.loads(self.calls[0].content)
        self.assertEqual(body["input"],PROMPT)
        self.assertEqual(body["tools"],[])
        self.assertEqual(body["tool_choice"],"none")
        self.assertFalse(body["store"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_output_tokens"],2048)
        self.assertEqual(body["truncation"],"disabled")
        log = " ".join(logs.output)
        self.assertIn("req_test",log)
        self.assertIn('"total_tokens": 18',log)
        for secret in ("test-only-secret","Private prospect reply","Thanks for your interest."):
            self.assertNotIn(secret,log)

    def test_client_disables_retries_and_fixes_api_origin(self):
        with patch.dict(os.environ,{"OPENAI_BASE_URL":"https://invalid.example"}):
            with adapter._client("test",5) as client:
                self.assertEqual(client.max_retries,0)
                self.assertEqual(str(client.base_url),"https://api.openai.com/v1/")
                self.assertEqual(client.timeout,5)

    def test_transient_statuses_attempt_once_and_hide_error_body(self):
        for status in (408,409,429,500,503):
            self.calls.clear()
            with self.subTest(status=status), self.assertRaises(RuntimeError) as err:
                self.invoke(status,{"error":{"message":"private-error","type":"server_error"}})
            self.assertEqual(len(self.calls),1)
            self.assertNotIn("private-error",str(err.exception))
            self.assertNotIsInstance(err.exception,PermanentModelError)

    def test_access_errors_stop_worker_configuration(self):
        for status in (401,403,404):
            with self.subTest(status=status), self.assertRaises(ModelConfigurationError):
                self.invoke(status,{"error":{"message":"private-error"}})

    def test_request_rejection_is_permanent(self):
        with self.assertRaises(PermanentModelError):
            self.invoke(400,{"error":{"message":"invalid prompt"}})

    def test_network_errors_are_sanitized(self):
        for error in (httpx.ConnectError,httpx.ReadTimeout):
            with self.subTest(error=error), self.assertRaises(RuntimeError) as err:
                self.invoke(error=lambda request: error("private-error",request=request))
            self.assertNotIn("private-error",str(err.exception))

    def test_incomplete_refusal_and_tool_calls_rejected(self):
        variants=[]
        incomplete=response_body(); incomplete["status"]="incomplete"; variants.append(incomplete)
        refusal=response_body(); refusal["output"][0]["content"]=[{"type":"refusal","refusal":"Cannot comply"}]; variants.append(refusal)
        tool=response_body(); tool["output"].append({"type":"function_call","id":"fc_test","name":"send","arguments":"{}","call_id":"call_test"}); variants.append(tool)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(PermanentModelError):
                self.invoke(payload=payload)

    def test_blank_oversized_and_marker_output_rejected(self):
        for body in (" ","x"*20001,"Hello <END_OF_TURN>"):
            payload=response_body(); payload["output"][0]["content"][0]["text"]=body
            with self.subTest(length=len(body)), self.assertRaises(PermanentModelError):
                self.invoke(payload=payload)

    def test_reasoning_is_not_returned(self):
        payload=response_body(); payload["output"].insert(0,{"id":"reason","type":"reasoning","summary":[]})
        self.assertEqual(self.invoke(payload=payload),"Thanks for your interest.")

    def test_missing_configuration_prevents_claim(self):
        for field in ENV:
            with patch.dict(os.environ,{field:""}):
                repo=Mock()
                worker=GenerationWorker(repo,ProcessGenerator("outreach_recovery.openai_adapter:generate",5))
                with self.assertRaises(ModelConfigurationError):
                    worker.run_once()
                repo.claim.assert_not_called()

    def test_invalid_limits_and_prompt_prevent_network(self):
        for limit in ("0","8193","NaN"):
            with patch.dict(os.environ,{"OUTREACH_OPENAI_MAX_OUTPUT_TOKENS":limit}), self.assertRaises(ModelConfigurationError):
                adapter.preflight()
        with patch.object(adapter,"_client") as client:
            with self.assertRaises(PermanentModelError):
                adapter.generate([{"role":"user","content":"x"}],timeout_seconds=5)
            client.assert_not_called()

    def test_cli_configuration_guard_exits_before_database_access(self):
        import subprocess
        import sys
        env = dict(os.environ, OPENAI_API_KEY="", OUTREACH_DATABASE_URL="invalid-dsn")
        result = subprocess.run([sys.executable, "-m", "outreach_recovery.generation",
            "--generator", "outreach_recovery.openai_adapter:generate", "--once"],
            env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn("configuration_error=missing_api_key",result.stderr)
        self.assertNotIn("Traceback",result.stderr)

    def test_configuration_error_survives_process_boundary(self):
        with self.assertRaisesRegex(ModelConfigurationError,"provider_access_denied"):
            ProcessGenerator(__name__+":subprocess_config_failure",5).generate(PROMPT)


@unittest.skipUnless(fixtures.DSN and fixtures.psycopg2,"Requires disposable PostgreSQL")
class OpenAIWorkerDatabaseTests(unittest.TestCase):
    setUp = fixtures.GenerationDatabaseTests.setUp
    sql = fixtures.GenerationDatabaseTests.sql
    ingest = fixtures.GenerationDatabaseTests.ingest
    state = fixtures.GenerationDatabaseTests.state

    def test_sdk_through_subprocess_saves_pending_review(self):
        with patch.dict(os.environ,ENV):
            worker=GenerationWorker(self.repo,ProcessGenerator(__name__+":subprocess_model",10))
            self.assertEqual(worker.run_once(),"drafted")
        self.assertEqual(self.state(),("completed",1))
        self.assertEqual(self.sql("SELECT state,body FROM outreach_pilot.drafts WHERE triggering_message_id=%s",(self.message,)),
                         [("pending_review","Thanks for your interest.")])

    def test_provider_configuration_failure_dead_letters_and_raises(self):
        worker=GenerationWorker(self.repo,ProcessGenerator(__name__+":subprocess_config_failure",5))
        with self.assertRaises(ModelConfigurationError):
            worker.run_once()
        self.assertEqual(self.state(),("dead_letter",1))
        self.assertIn("provider_access_denied",self.sql("SELECT last_error FROM outreach_pilot.reply_jobs WHERE id=%s",(self.job_id,))[0][0])

    def test_synthetic_runner_processes_only_its_own_job(self):
        from outreach_recovery.smoke_openai import run_synthetic_job
        generator=fixtures.CallbackGenerator(lambda _: "Synthetic reply for review")
        result=run_synthetic_job(fixtures.DSN,generator)
        self.assertTrue(result["success"])
        self.assertEqual(result["draft_state"],"pending_review")
        self.assertEqual(self.state(),("pending",0))

    def test_synthetic_runner_does_not_retry_transient_failures(self):
        from outreach_recovery.smoke_openai import run_synthetic_job
        def fail(_):
            raise TimeoutError()
        result=run_synthetic_job(fixtures.DSN,fixtures.CallbackGenerator(fail))
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"],1)
        self.assertIsNone(result["body"])
