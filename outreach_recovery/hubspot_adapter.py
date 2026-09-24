"""Bounded CRM email logging. No email sending; uncertain writes are never retried."""
import json
import time
import base64
from email.parser import BytesParser
from email import policy
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import httpx
from outreach_recovery.delivery_contracts import MutationResult, MutationState, ReconciliationResult, ReconciliationState


def timestamp_ms(value):
    text = str(value)
    if text.isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return int(parsed.timestamp() * 1000)


def email_properties(payload):
    payload = dict(payload)
    # Existing approved delivery envelopes pin Date in raw MIME.
    if "timestamp" not in payload and payload.get("raw_mime"):
        raw = payload["raw_mime"]
        mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        payload["timestamp"] = parsedate_to_datetime(str(mime["Date"])).isoformat()
    direction = payload.get("direction", "outbound")
    if direction not in ("inbound", "outbound"):
        raise ValueError("Invalid email direction")
    for field in ("from", "to", "subject", "body", "timestamp", "hubspot_portal_id", "hubspot_contact_id"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError("Incomplete activity payload")
    if not payload["hubspot_contact_id"].isdigit():
        raise ValueError("Invalid contact")
    result = {"hs_timestamp": str(timestamp_ms(payload["timestamp"])),
              "hs_email_direction": "EMAIL" if direction == "outbound" else "INCOMING_EMAIL",
              "hs_email_subject": payload["subject"], "hs_email_text": payload["body"],
              "hs_email_headers": json.dumps({"from": {"email": payload["from"]},
                    "to": [{"email": payload["to"]}], "cc": [], "bcc": []})}
    if direction == "outbound":
        result["hs_email_status"] = "SENT"
    return result


class HubSpotAdapter:
    def __init__(self, *, portal_id, token_provider, transport=None):
        self.portal_id = str(portal_id)
        self.token_provider = token_provider
        self.transport = transport

    def _request(self, client, deadline, method, path, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provider deadline")
        response = client.request(method, "https://api.hubapi.com" + path,
                                  timeout=remaining, **kwargs)
        return response

    def _context(self, payload, timeout_seconds):
        email_properties(payload)
        if payload["hubspot_portal_id"] != self.portal_id or timeout_seconds <= 0:
            raise ValueError("Portal mismatch or invalid deadline")
        return httpx.Client(transport=self.transport or httpx.HTTPTransport(retries=0),
                            follow_redirects=False, trust_env=False,
                            headers={"Authorization": "Bearer " + self.token_provider()})

    def _verify(self, client, deadline, payload):
        account = self._request(client, deadline, "GET", "/account-info/v3/details")
        if account.status_code != 200 or str(account.json().get("portalId")) != self.portal_id:
            raise ValueError("Account verification failed")
        contact = self._request(client, deadline, "GET", "/crm/v3/objects/contacts/" + payload["hubspot_contact_id"], params={"properties": "email"})
        expected = payload["to"] if payload.get("direction", "outbound") == "outbound" else payload["from"]
        if contact.status_code != 200 or contact.json().get("properties", {}).get("email", "").lower() != expected.lower():
            raise ValueError("Contact verification failed")

    def _lookup(self, client, deadline, payload):
        props = email_properties(payload)
        query = {"filterGroups": [{"filters": [
            {"propertyName": "hs_email_subject", "operator": "EQ", "value": props["hs_email_subject"]},
            {"propertyName": "hs_email_direction", "operator": "EQ", "value": props["hs_email_direction"]},
            {"propertyName": "hs_timestamp", "operator": "EQ", "value": props["hs_timestamp"]}]}],
            "properties": ["hs_email_text", "hs_email_from_email", "hs_email_to_email"], "limit": 100}
        response = self._request(client, deadline, "POST", "/crm/v3/objects/emails/search", json=query)
        if response.status_code != 200:
            return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
        data = response.json()
        if data.get("paging", {}).get("next") or data.get("total", 0) > 100:
            return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
        matches = []
        for item in data.get("results", []):
            p = item.get("properties", {})
            if (p.get("hs_email_text") != payload["body"] or
                p.get("hs_email_from_email", "").lower() != payload["from"].lower() or
                p.get("hs_email_to_email", "").lower() != payload["to"].lower()):
                continue
            identifier = str(item.get("id", ""))
            if not identifier.isdigit():
                return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
            associated = self._request(client, deadline, "GET", "/crm/v3/objects/emails/" + identifier,
                                       params={"associations": "contacts"})
            if associated.status_code != 200:
                return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
            ids = {str(x["id"]) for x in associated.json().get("associations", {}).get("contacts", {}).get("results", [])}
            if payload["hubspot_contact_id"] in ids:
                matches.append(identifier)
        if len(matches) == 1:
            return ReconciliationResult(ReconciliationState.FOUND, matches[0])
        return ReconciliationResult(ReconciliationState.LOOKUP_FAILED if matches else ReconciliationState.NOT_FOUND_YET)

    def reconcile(self, payload, *, operation_id, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        try:
            with self._context(payload, timeout_seconds) as client:
                self._verify(client, deadline, payload)
                return self._lookup(client, deadline, payload)
        except Exception:
            return ReconciliationResult(ReconciliationState.LOOKUP_FAILED)

    def create_email(self, payload, *, operation_id, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        mutation_started = False
        try:
            with self._context(payload, timeout_seconds) as client:
                self._verify(client, deadline, payload)
                existing = self._lookup(client, deadline, payload)
                if existing.state == ReconciliationState.FOUND:
                    return MutationResult(MutationState.ACCEPTED, existing.provider_id)
                if existing.state != ReconciliationState.NOT_FOUND_YET:
                    return MutationResult(MutationState.REJECTED, safe_to_retry=True)
                body = {"properties": email_properties(payload), "associations": [{
                    "to": {"id": payload["hubspot_contact_id"]},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}]}]}
                mutation_started = True
                response = self._request(client, deadline, "POST", "/crm/v3/objects/emails", json=body)
                if response.status_code in (200, 201):
                    identifier = str(response.json().get("id", ""))
                    if identifier.isdigit():
                        return MutationResult(MutationState.ACCEPTED, identifier)
                # A gateway/server failure may occur after acceptance. Never retry it.
                if response.status_code in (400, 401, 403, 404, 422, 429):
                    return MutationResult(MutationState.REJECTED, safe_to_retry=response.status_code == 429)
                return MutationResult(MutationState.UNCERTAIN)
        except Exception:
            return MutationResult(MutationState.UNCERTAIN if mutation_started else MutationState.REJECTED)
