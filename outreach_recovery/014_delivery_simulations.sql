BEGIN;
CREATE TABLE outreach_pilot.delivery_simulations (
 operation_id uuid PRIMARY KEY REFERENCES outreach_pilot.delivery_operations(id),
 outcome text NOT NULL CHECK(outcome IN ('simulated_completed','blocked_allowlist','blocked_stale','reconciliation_only')),
 mock_gmail_id text, mock_crm_id text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE outreach_pilot.delivery_simulations IS 'Test-only mock outbox. Never a provider delivery receipt.';
COMMIT;
