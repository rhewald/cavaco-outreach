# Review queue

Apply `012_review_context.sql` after migrations 001–011 before starting the updated review server. It adds display-only contact metadata; it does not change approval or send permissions.

The queue supports literal case-insensitive search across name, company, email and subject; message-type/mailbox filters; oldest/newest/prospect/company sorting; and 50-row pagination that retains filters. Times are UTC. Reply generation timestamps are used where available; initial outreach falls back to conversation update time.

A triggering inbound message identifies a reply. A draft with no triggering message and no prior ledger messages is first outreach (message 0); otherwise it is a follow-up. Counts cover ledger messages through the draft's version snapshot.

Refresh enrolled contacts' display names and companies using:

```sh
python -m outreach_recovery.refresh_review_contacts
```

This reads HubSpot using the configured portal token and writes only the local display cache. It verifies contact email against the approved route. Company comes from the contact company property or a single associated company. Missing or ambiguous company context is displayed as unavailable. Refresh is explicit, not a live CRM request during page rendering. `OUTREACH_DATABASE_URL` overrides the local pilot database.

Search and display fields never authorize recipients or change pinned delivery envelopes. Existing transactional approval/version checks remain in effect.

Apply `013_review_contact_links.sql` for contact profile links and phone display, then refresh the contact cache. Both queue and detail pages show the HubSpot profile from the approved route, a validated LinkedIn personal-profile URL, and available phone/mobile/WhatsApp/additional-number values. Missing fields are labeled explicitly. External links open in a new tab. CRM data is display-only and remains outside generation/approval payloads.
