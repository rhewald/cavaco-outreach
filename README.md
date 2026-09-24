# Cavaco Outreach

Supervised sales outreach with HubSpot as the business source of truth, Gmail for email delivery, and PostgreSQL for durable processing.

## Current status

This repository contains PostgreSQL inbound ingestion, leased draft generation, seller-profile validation, a live-tested direct OpenAI adapter, the Cavaco Outreach review interface, and a durable delivery outbox with provider contracts and crash-recovery tests. It is not a deployed service. The Gmail REST adapter is implemented and tested offline; OAuth onboarding and live verification remain pending. HubSpot, OpenSales, and SalesGPT adapters remain to be connected. The SQL and integration tests run against disposable PostgreSQL 16.2.

The [delivery milestone guide](outreach_recovery/DELIVERY.md) documents approval-bound send intents, independent CRM operations, reconciliation, and remaining live integration work.

## Run the tests

Requires Python 3.9 or newer and the test dependencies in the component guides; no live API credentials are needed.

```sh
python3 -m unittest discover -s outreach_recovery -p 'test_*.py' -v
```

For database tests and parameter mapping, see the [inbound transaction guide](outreach_recovery/INBOUND_TRANSACTIONS.md).

## Draft generation

See the [generation-worker guide](outreach_recovery/GENERATION_WORKER.md) for migration order, approved seller configuration, the model adapter contract, running the poller, and inspecting failed jobs.

## Project files

- [Worker and recovery contracts](outreach_recovery/worker.py)
- [Crash-recovery tests](outreach_recovery/test_worker.py)
- [PostgreSQL migration](outreach_recovery/inbound.sql)
- [SQL verification cases](outreach_recovery/inbound_checks.sql)
- [Inbound transaction guide](outreach_recovery/INBOUND_TRANSACTIONS.md)
- [Recovery design and implementation gaps](outreach_recovery/README.md)

## Next milestone

Complete one supervised lead loop: research and record a contact in HubSpot, approve an outreach draft, send through Gmail, receive and match the reply, generate a follow-up draft, and synchronize activity to HubSpot.

Before live operation, implement Gmail and HubSpot adapters, mailbox notification ingestion, configure real approved seller content, and complete supervised live integration tests. Uncertain send outcomes must remain in reconciliation or manual review; an empty search result must never automatically authorize a repeat send.

## Origin

Extracted from the local Cavaco AI MCP workspace on September 24, 2026. This repository owns the outreach code going forward.

Seller profiles now have versioned database validation; apply migration
`outreach_recovery/004_seller_validation.sql` after 003. See
[outreach_recovery/GENERATION_WORKER.md](outreach_recovery/GENERATION_WORKER.md)
for pricing rules, example facts, and legacy profile recovery limits.

Direct OpenAI generation is implemented behind the existing callable contract.
See [OpenAI adapter setup and one-job live smoke test](outreach_recovery/OPENAI_ADAPTER.md).
The user completed a live gpt-4o-mini synthetic smoke test successfully on 2026-09-24.

The [local human-review interface](outreach_recovery/HUMAN_REVIEW.md) supports
version-checked approval, rejection, and an immutable decision audit. Migration
005 is required. Gmail delivery and HubSpot synchronization are still pending.

## Gmail adapter

See [Gmail adapter scope and setup](outreach_recovery/GMAIL_ADAPTER.md). No live mailbox or send is enabled by installing this code.
