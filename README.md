# Cavaco Outreach

Supervised sales outreach with HubSpot as the business source of truth, Gmail for email delivery, and PostgreSQL for durable processing.

## Current status

This repository contains the email recovery policy, atomic inbound transactions, a leased draft-generation worker, a versioned context builder, and simulation plus PostgreSQL integration tests. It is not a deployed service. Live Gmail, HubSpot, OpenSales, and model/SalesGPT provider adapters remain to be connected. The SQL and integration tests have been exercised against a disposable PostgreSQL 16.2 instance.

## Run the tests

Requires Python 3.9 or newer; no external packages or credentials are needed.

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

Before live operation, implement the PostgreSQL repository, Gmail and HubSpot adapters, mailbox notification ingestion, seller content configuration, approval interface, and real integration tests. Uncertain send outcomes must remain in reconciliation or manual review; an empty search result must never automatically authorize a repeat send.

## Origin

Extracted from the local Cavaco AI MCP workspace on September 24, 2026. This repository owns the outreach code going forward.
