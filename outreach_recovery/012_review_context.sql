BEGIN;
-- Display-only contact context; never used to authorize delivery or model claims.
CREATE TABLE outreach_pilot.review_contacts (
 conversation_id uuid PRIMARY KEY REFERENCES outreach_pilot.conversations(id),
 display_name text NOT NULL DEFAULT '', company_name text NOT NULL DEFAULT '',
 source text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;
