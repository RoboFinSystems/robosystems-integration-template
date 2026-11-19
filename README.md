# RoboSystems Integration Template

Part of RoboSystems, the open-source knowledge graph for finance. A scaffold for building custom integrations against the [RoboSystems](https://github.com/RoboFinSystems/robosystems) public API.

**Living example**: [`robosystems-marketing-integration`](https://github.com/RoboFinSystems/robosystems-marketing-integration) — a real integration built from this template that collects RFS's own marketing/usage metrics (GitHub, npm, PyPI, Docker Hub) and asserts them as a monthly metric series.

An integration lives in its own repository — this one — and speaks to the platform exclusively through the public API with an API key. It never runs inside the platform, so it survives every platform release, works identically against the managed cloud or a self-hosted deployment, and is yours to run anywhere: a cron job, a Lambda, a container, a GitHub Actions schedule.

## The three lanes

Pick the lane that matches your data's nature (an integration can use more than one):

| Lane | What you send | What the platform enforces | Emitter |
|---|---|---|---|
| **1 — Ledger** | Business events → the general ledger | Double-entry balance, capture-then-approve, closed-period gate, `(source, external_id)` idempotency | `emit/events.py` |
| **1, mirrored — A ledger kept elsewhere** | Your ERP's journal → read-only books | The same balance and closed-period gate; the books take your entries and nothing else | `emit/ledger.py` |
| **2 — Semantic facts** | A custom vocabulary + observed metric series | Typed concepts, presentation structure, per-period replace, provenance | `emit/metrics.py` |
| **3 — Raw graph** | Parquet/CSV files → staging → graph | Per-graph schema, bulk ingestion pipeline | `emit/graph.py` |

- **Lane 1** is for data that *is* accounting: invoices, payments, payroll from a system the platform has no adapter for. Events land in an inbox (`captured`), an operator approves, handlers derive the GL entries.
- **Lane 1, mirrored** is for a source that *is* the books: an ERP or accounting system whose general ledger the graph mirrors for reporting and analysis. The source registers as the ledger that keeps an entity's books; its journal entries land posted with no approval step, nothing posts to those books on the platform, nothing is written back to your system, and closing a period stamps it without posting. See [A ledger kept elsewhere](#a-ledger-kept-elsewhere).
- **Lane 2** is for data that is *facts but not bookkeeping*: marketing counts, usage numbers, operational KPIs. Author your vocabulary once (`create-taxonomy-block`), then assert observed values per period (`assert-metrics`). The platform renders the series everywhere — envelopes, charts, fact grids, GraphQL, MCP — with no further work.
- **Lane 3** is for graph-shaped domain data: upload files, stage, materialize. Combined with the per-graph schema operations you can stand up a complete custom knowledge graph.

## Quickstart

1. Click **Use this template** and create your integration repo (typically private).
2. Clone it, then:

```bash
just venv         # environment + dependencies + git hooks (.env provisioned from .env.example)
# fill in API key + graph id in .env
just run          # collect → transform → emit
```

3. Replace the stubs in `src/integration/collect.py` and `transform.py` with your source's extract/transform, and wire the emitter(s) for your lane in `main.py`.

Day-to-day commands mirror the other RoboSystems Python repos: `just test`, `just test-all` (tests + format + lint + typecheck — the CI gate), `just lint`, `just format`, `just typecheck`, `just create-feature <type> <name>`.

## Layout

```
src/integration/
  config.py       # env-driven settings (.env supported)
  client.py       # robosystems-client SDK wired with API-key auth,
                  # plus envelope unwrapping + a raw-op escape hatch
  collect.py      # YOUR extract — pull from your source system
  transform.py    # YOUR transform — shape raw data for the lane you use
  emit/
    events.py     # lane 1: create-event-block
    ledger.py     # lane 1, mirrored: a ledger kept elsewhere, read-only
    metrics.py    # lane 2: create-taxonomy-block + assert-metrics
    graph.py      # lane 3: create-file-upload → ingest-file → materialize
  main.py         # collect → transform → emit
```

The split is deliberate: **you own extract and transform** (they're specific to your source); **the platform owns validation and load** (the operations behind the API). Credentials for *your source system* stay on your side — the platform never holds them.

## Conventions that matter

- **Idempotency**: lane-1 events carry `(source, external_id)`, which is unique on the platform, so a re-send never posts twice. The API answers 409 and `emit_event` returns `{"already_ingested": True, ...}` instead of raising, so retries are safe and a re-run batch carries on. Operations also accept an `Idempotency-Key` header (the client sends one when you pass it).
- **Raw history is yours**: keep your raw pulls (e.g. daily JSON snapshots) in your own storage; send the platform the rolled-up series at your reporting cadence. Backfill is one loop over your history.
- **One source name per integration**: your integration's registered source name is its identity stamp on everything it writes.

## A ledger kept elsewhere

`emit/ledger.py` mirrors a general ledger the platform does not keep. One source keeps one entity's books, so a group with several companies runs the integration once per company, each with its own `INTEGRATION_SOURCE_NAME`.

```python
from datetime import date

from integration.emit.ledger import (
  emit_journal_entry,
  record_opening_balance,
  register_ledger,
  report_sync,
  withdraw_entry,
)

# Every run: returns the connection it already has after the first.
connection = register_ledger(client, entity_id=entity_id, display_name="ERP (UK)")

# Once: the balances the mirror starts from, so earlier history stays home.
record_opening_balance(client, opening_balances, entity_id=entity_id)

# Each entry, with the source's version of it.
sent = emit_journal_entry(
  client,
  external_id="JE-1042",
  version=3,
  posting_date=date(2026, 9, 20),
  lines=[
    {"element_id": cash, "debit_amount": 62500, "credit_amount": 0},
    {"element_id": revenue, "debit_amount": 0, "credit_amount": 62500},
  ],
  entity_id=entity_id,
)

# An entry the source deleted, by the event id its first delivery returned.
withdraw_entry(client, event_id)

# Last: so a period close can tell the mirror is current.
report_sync(client, connection["connection_id"], counts={"entries_sent": 1})
```

What to keep in mind:

- **Version every entry.** A higher `version` of an `external_id` already sent replaces the entry, whole. The same or a lower version is a repeat and changes nothing, so a re-run is safe. Use the source's revision counter, or its last-modified time as an integer.
- **Keep the event id.** The first delivery's `result["id"]` is what `withdraw_entry` takes; store it beside the source id in your own state.
- **Corrections reach open periods only.** A change dated in a closed month is refused. Reopen the month, send the change, and close it again; on these books a close posts nothing.
- **Accounts are the entity's own.** Lines name accounts by element id in that entity's chart of accounts, which you set up before the first run.
- **Needs `robosystems-client` 2.41 or later**, and an API that has the external ledger connection (self-hosted deployments: the release that added `ledger` to the external connection).

## Deploying

An integration is a small program that runs on a schedule — the contract is env vars in, API calls out — so the runtime is swappable. The ladder, in increasing weight:

1. **GitHub Actions (the default, included)**: `.github/workflows/run.yml` runs the integration on a cron with zero infrastructure. Set `secrets.ROBOSYSTEMS_API_KEY` plus `vars.ROBOSYSTEMS_GRAPH_ID` / `vars.INTEGRATION_SOURCE_NAME` in the repo settings and it's deployed. Standard runners give 4 vCPU / 16 GB and a 6-hour cap — ample for API-pull collectors.
2. **GitHub Actions, larger runner**: heavy backfills or transforms get real memory/CPU with a one-line `runs-on` change (e.g. `ubuntu-latest-8-cores`) — still zero infrastructure.
3. **ECS Fargate scheduled task**: full resource control and CloudWatch visibility when an integration outgrows runners — EventBridge Scheduler → `RunTask`, no service or load balancer, public subnet with egress only (the integration never needs to be inside a VPC; the API is the boundary). This template doesn't ship the CloudFormation for it — bring it when you graduate.

Secrets (API key, source credentials) belong in your runtime's secret store — repo secrets, or the task's secret manager — never in the repo.

## SDK

The [`robosystems-client`](https://pypi.org/project/robosystems-client/) Python SDK is how the emitters interface with the platform — typed request/response models and one generated function per operation (`robosystems_client.api.*`), regenerated from the live [OpenAPI spec](https://api.robosystems.ai/openapi.json). Brand-new operations occasionally land on the API before the SDK's next regeneration; `IntegrationClient.raw_operation` reaches those through the same authenticated client until the typed function exists.

Which parts of the SDK stay stable across releases is covered in [Versioning and Compatibility](https://robosystems.ai/docs/technical/versioning-and-compatibility#the-two-tier-sdk-contract).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

MIT © 2026 RFS LLC

