BEGIN;
ALTER TABLE outreach_pilot.review_contacts ADD COLUMN linkedin_url text NOT NULL DEFAULT '', ADD COLUMN phones jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(phones)='array');
COMMIT;
