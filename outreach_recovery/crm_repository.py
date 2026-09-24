"""Independent CRM lane for immutable sent/received messages; approval precedes claiming."""
import hashlib
import json
from uuid import uuid4
from psycopg2.extras import Json, RealDictCursor
from outreach_recovery.generation import GenerationRepository
from outreach_recovery.hubspot_adapter import email_properties


def payload_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CRMRepository(GenerationRepository):
    def prepare(self, message_id, *, portal_id, contact_id, sender, recipient, subject):
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM outreach_pilot.messages WHERE id=%s", (message_id,))
            message = cur.fetchone()
            if not message:
                raise ValueError("Unknown source message")
            payload = {"hubspot_portal_id": str(portal_id), "hubspot_contact_id": str(contact_id),
                       "from": sender, "to": recipient, "subject": subject,
                       "body": message["body_text"], "direction": message["direction"],
                       "timestamp": message["received_at"].isoformat()}
            email_properties(payload)
            cur.execute("""INSERT INTO outreach_pilot.crm_activity_jobs(message_id,portal_id,contact_id,payload)
                 VALUES(%s,%s,%s,%s) ON CONFLICT(message_id,portal_id) DO NOTHING""",
                 (message_id,str(portal_id),str(contact_id),Json(payload)))
            cur.execute("SELECT * FROM outreach_pilot.crm_activity_jobs WHERE message_id=%s AND portal_id=%s", (message_id,str(portal_id)))
            job = cur.fetchone()
            if job["payload"] != payload:
                raise ValueError("An immutable intent already exists with different content")
            return job

    def enable_reply_logging(self, completed_job_id):
        """Enroll the conversation using a verified, already-approved CRM activity."""
        from outreach_recovery.crm_handoff import register_route
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT j.*,m.conversation_id FROM outreach_pilot.crm_activity_jobs j
                JOIN outreach_pilot.messages m ON m.id=j.message_id WHERE j.id=%s AND j.state='completed'""",
                (completed_job_id,))
            job=cur.fetchone()
            if not job:
                raise ValueError("Completed approved activity required")
            p=job['payload'];outgoing=p['direction']=='outbound'
            register_route(cur,job['conversation_id'],portal_id=job['portal_id'],contact_id=job['contact_id'],
                contact_email=p['to'] if outgoing else p['from'],mailbox_email=p['from'] if outgoing else p['to'],
                reviewer=job['approved_by'],approval_reference='crm_activity:'+str(job['id']))
            return str(job['conversation_id'])

    def approve(self, job_id, *, reviewer, digest):
        if not reviewer.strip():
            raise ValueError("Reviewer required")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM outreach_pilot.crm_activity_jobs WHERE id=%s FOR UPDATE", (job_id,))
            job = cur.fetchone()
            if not job or payload_digest(job["payload"]) != digest:
                raise ValueError("Preview does not match intent")
            if job["state"] != "awaiting_approval":
                return False
            cur.execute("UPDATE outreach_pilot.crm_activity_jobs SET state='pending',approved_by=%s,approved_at=clock_timestamp() WHERE id=%s", (reviewer,job_id))
            self._event(cur,job,"approved")
            return True

    def _event(self, cur, job, event):
        cur.execute("INSERT INTO outreach_pilot.crm_activity_events(job_id,event,lease_token) VALUES(%s,%s,%s)", (job["id"],event,job.get("lease_token")))

    def claim(self, *, kind, operation_id=None, lease_seconds=120, portal_id=None):
        if kind != "hubspot_log" or not 10 <= lease_seconds <= 3600:
            raise ValueError("Invalid CRM claim")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM outreach_pilot.crm_activity_jobs WHERE
                 (%s::uuid IS NULL OR id=%s::uuid) AND (%s::text IS NULL OR portal_id=%s) AND
                 ((state IN ('pending','reconciliation_required') AND next_attempt_at<=clock_timestamp())
                  OR (state='processing' AND lease_until<=clock_timestamp()))
                 ORDER BY next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""", (operation_id,operation_id,portal_id,portal_id))
            job = cur.fetchone()
            if not job:
                return None
            if job["attempts"] >= 8:
                cur.execute("UPDATE outreach_pilot.crm_activity_jobs SET state='manual_review',lease_token=NULL,lease_until=NULL WHERE id=%s", (job["id"],))
                self._event(cur,job,"manual_review")
                return None
            recovery = job["state"] != "pending" or job["started_at"] is not None
            cur.execute("""UPDATE outreach_pilot.crm_activity_jobs SET state='processing',lease_token=%s,
                lease_until=clock_timestamp()+make_interval(secs=>%s),attempts=attempts+1 WHERE id=%s RETURNING *""",
                (str(uuid4()),lease_seconds,job["id"]))
            job = cur.fetchone()
            job["recovery"] = recovery
            self._event(cur,job,"claimed_reconcile" if recovery else "claimed_create")
            return job

    def _owned(self, cur, claim):
        cur.execute("""SELECT * FROM outreach_pilot.crm_activity_jobs WHERE id=%s AND state='processing'
            AND lease_token=%s AND lease_until>clock_timestamp() FOR UPDATE""", (claim["id"],claim["lease_token"]))
        return cur.fetchone()

    def begin(self, claim):
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            job = self._owned(cur,claim)
            if not job or claim["recovery"] or job["started_at"]:
                return False
            cur.execute("UPDATE outreach_pilot.crm_activity_jobs SET started_at=clock_timestamp() WHERE id=%s", (job["id"],))
            self._event(cur,job,"started")
            return True

    def complete(self, claim, provider_id, thread_id=None):
        if not isinstance(provider_id,str) or not provider_id.isdigit():
            raise ValueError("Invalid provider ID")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            job = self._owned(cur,claim)
            if not job:
                return False
            cur.execute("UPDATE outreach_pilot.crm_activity_jobs SET state='completed',provider_id=%s,lease_token=NULL,lease_until=NULL WHERE id=%s", (provider_id,job["id"]))
            self._event(cur,job,"completed")
            return True

    def defer(self, claim, event, *, rejected=False, safe_to_retry=False):
        if event not in ("uncertain","rejected","not_found_yet","lookup_failed"):
            raise ValueError("Invalid outcome")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            job = self._owned(cur,claim)
            if not job:
                return False
            state = "reconciliation_required"
            if rejected:
                state = "pending" if safe_to_retry else "rejected"
            if job["attempts"] >= 8 and state != "rejected":
                state = "manual_review"
            cur.execute("""UPDATE outreach_pilot.crm_activity_jobs SET state=%s,lease_token=NULL,lease_until=NULL,
                started_at=CASE WHEN %s THEN NULL ELSE started_at END,
                next_attempt_at=clock_timestamp()+make_interval(secs=>%s) WHERE id=%s""",
                (state,rejected and safe_to_retry,min(3600,10*2**(job["attempts"]-1)),job["id"]))
            self._event(cur,job,event)
            return True
