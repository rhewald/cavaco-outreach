"""Direct OpenAI Responses adapter. No database, delivery, or CRM capabilities."""
import json
import logging
import math
import os
import re
import time

from outreach_recovery.generation import GENERATION_CONTEXT, ModelConfigurationError, PermanentModelError

LOG = logging.getLogger(__name__)


def preflight():
    """Local validation only; it does not establish remote key/model access."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OUTREACH_OPENAI_MODEL", "").strip()
    if not key:
        raise ModelConfigurationError("missing_api_key")
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", model):
        raise ModelConfigurationError("missing_or_invalid_model")
    try:
        limit = int(os.environ.get("OUTREACH_OPENAI_MAX_OUTPUT_TOKENS", "2048"))
    except ValueError:
        raise ModelConfigurationError("invalid_output_limit") from None
    if not 64 <= limit <= 8192:
        raise ModelConfigurationError("invalid_output_limit")
    try:
        import openai  # noqa: F401
    except ImportError:
        raise ModelConfigurationError("missing_sdk") from None
    return key, model, limit


def _client(key, timeout_seconds):
    from openai import OpenAI
    # SDK debug logs can contain request bodies; own telemetry below is allowlisted.
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return OpenAI(api_key=key, base_url="https://api.openai.com/v1",
                  max_retries=0, timeout=timeout_seconds)


def _body(response):
    if response.status != "completed":
        raise PermanentModelError("incomplete_or_failed_response")
    parts = []
    for item in response.output:
        if item.type == "reasoning":
            continue
        if item.type != "message" or item.role != "assistant" or item.status != "completed":
            raise PermanentModelError("unexpected_output_item")
        for part in item.content:
            if part.type != "output_text":
                raise PermanentModelError("refusal_or_nontext_output")
            parts.append(part.text)
    text = "\n".join(parts).strip()
    if not text or len(text)>20000 or any(m in text for m in
            ("<END_OF_TURN>", "<END_OF_CALL>", "<BOT>", "</BOT>")):
        raise PermanentModelError("invalid_body")
    return text


def generate(messages, *, timeout_seconds):
    import openai
    key, model, limit = preflight()
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ModelConfigurationError("invalid_timeout")
    if (not isinstance(messages, list) or len(messages) != 2
            or any(not isinstance(m, dict) for m in messages)
            or [m.get("role") for m in messages] != ["system", "user"]
            or any(not isinstance(m.get("content"), str) or not m["content"].strip() for m in messages)
            or sum(len(m["content"]) for m in messages)>100000):
        raise PermanentModelError("invalid_prompt")
    # Rebuild the request so callers cannot inject tools or additional API fields.
    inputs = [{"role":m["role"], "content":m["content"]} for m in messages]
    started = time.monotonic()
    metrics = {"model":model, "outcome":"error", **GENERATION_CONTEXT.get()}
    try:
        with _client(key, timeout_seconds) as client:
            response = client.responses.create(model=model, input=inputs,
                max_output_tokens=limit, tools=[], tool_choice="none",
                store=False, stream=False, truncation="disabled")
        request_id = getattr(response, "_request_id", None)
        if isinstance(request_id,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}",request_id):
            metrics["request_id"] = request_id
        usage = getattr(response,"usage",None)
        if usage:
            for field in ("input_tokens","output_tokens","total_tokens"):
                value = getattr(usage,field,None)
                if type(value) is int and value>=0:
                    metrics[field] = value
        try:
            body = _body(response)
        except (AttributeError, TypeError, ValueError):
            raise PermanentModelError("malformed_response") from None
        metrics["outcome"] = "completed"
        return body
    except openai.APIStatusError as exc:
        metrics["http_status"] = exc.status_code
        if exc.status_code in (401,403,404):
            metrics["outcome"] = "configuration_error"
            raise ModelConfigurationError("provider_access_denied") from None
        if exc.status_code in (408,409,429) or exc.status_code>=500:
            metrics["outcome"] = "retry"
            raise RuntimeError("Transient provider failure") from None
        metrics["outcome"] = "permanent"
        raise PermanentModelError("provider_rejected_request") from None
    except (openai.APIConnectionError, openai.APITimeoutError):
        metrics["outcome"] = "retry"
        raise RuntimeError("Provider connection or timeout failure") from None
    except PermanentModelError:
        metrics["outcome"] = "permanent"
        raise
    finally:
        metrics["duration_ms"] = round((time.monotonic()-started)*1000)
        LOG.info("openai_generation %s", json.dumps(metrics,sort_keys=True))


# Optional callable hook recognized by ProcessGenerator before acquiring work.
generate.preflight = preflight
