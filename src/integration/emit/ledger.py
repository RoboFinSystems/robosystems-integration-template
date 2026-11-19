"""Lane 1, mirrored — a general ledger kept elsewhere, read-only on the platform.

Use this when the source *is* the books: an ERP or accounting system whose
journal the graph mirrors for reporting and analysis. The source registers
as the ledger that keeps an entity's books, and from then on those books
take its entries and nothing else: nothing posts to them on the platform,
nothing is written back, and closing a period stamps it without posting.

A run, in order:

1. ``register_ledger`` — once, and safe to repeat on every run.
2. ``record_opening_balance`` — once: the balances the mirror starts from,
   so history before it need not be sent.
3. ``emit_journal_entry`` per journal entry, each with the source's
   ``version`` of it. A higher version of an entry already sent replaces
   it; the same version again is a repeat and changes nothing.
4. ``withdraw_entry`` for an entry the source no longer holds, by the id
   the source sent it under.
5. ``report_sync`` — at the end, so a period close can tell the mirror is
   current.

One source keeps one entity's books: a group registers a source name per
company. Corrections reach open periods only; reopen a closed month, send
the change, and close it again.

Only ``emit_journal_entry`` goes through the SDK's typed operation (the
same ``create-event-block`` every lane-1 integration uses). The other
calls are made through the authenticated HTTP client by path.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from integration.client import IntegrationClient
from integration.emit.events import emit_event

JOURNAL_ENTRY = "journal_entry_recorded"


def register_ledger(
  client: IntegrationClient,
  *,
  entity_id: str | None = None,
  display_name: str | None = None,
) -> dict:
  """Register this integration's source as the ledger keeping
  ``entity_id``'s books (the group parent's when omitted); returns the
  connection, whose ``connection_id`` ``report_sync`` takes.

  Registering the same source again returns the connection it already
  has, so a run can call this first every time. A 409 means another
  ledger keeps the entity's books or the entity has books of its own.
  """
  config: dict[str, Any] = {"source_name": client.config.source_name, "ledger": True}
  if entity_id:
    config["entity_id"] = entity_id
  if display_name:
    config["display_name"] = display_name
  return client.raw_operation(
    f"/v1/graphs/{client.config.graph_id}/connections",
    {"provider": "external", "external_config": config},
  )


def record_opening_balance(
  client: IntegrationClient,
  balances: list[dict[str, Any]],
  *,
  entity_id: str | None = None,
  equity_element_id: str | None = None,
  memo: str | None = None,
) -> dict:
  """Record the balances the mirror starts from, as of the day before the
  entity's first fiscal period. Each balance is
  ``{"element_id", "debit_amount", "credit_amount"}`` in cents;
  ``equity_element_id`` takes the difference when they do not balance.
  It can be recorded again until the entity's first period is closed.
  """
  body: dict[str, Any] = {"balances": balances}
  if entity_id:
    body["entity_id"] = entity_id
  if equity_element_id:
    body["equity_element_id"] = equity_element_id
  if memo:
    body["memo"] = memo
  return _operation(client, "record-opening-balance", body)


def emit_journal_entry(
  client: IntegrationClient,
  *,
  external_id: str,
  version: int,
  posting_date: date,
  lines: list[dict[str, Any]],
  memo: str | None = None,
  entity_id: str | None = None,
  dimension_ids: list[str] | None = None,
) -> dict:
  """Send one journal entry of the source's, or a later version of it.

  ``external_id`` is the entry's id in the source and ``version`` a number
  the source only increases when the entry changes (a revision counter, or
  its last-modified time as an integer). ``lines`` are
  ``{"element_id", "debit_amount", "credit_amount"}`` in cents and must
  balance. ``memo`` defaults to ``external_id``.

  The entry lands posted. Sent again with a higher version it replaces
  the earlier one, whole: lines, date, memo and dimensions. Sent again
  with the same or a lower version it returns
  ``{"already_ingested": True, ...}`` and nothing changes.

  """
  if version < 1:
    raise ValueError("version starts at 1 and only grows")
  debits = sum(int(line.get("debit_amount") or 0) for line in lines)
  payload: dict[str, Any] = {
    "event_type": JOURNAL_ENTRY,
    "event_category": "adjustment",
    "external_id": external_id,
    "source_version": version,
    "occurred_at": f"{posting_date.isoformat()}T00:00:00Z",
    "amount": debits,
    "apply_handlers": True,
    "metadata": {
      "posting_date": posting_date.isoformat(),
      "status": "posted",
      "line_items": lines,
      # The platform requires a memo; the source's own id is the fallback.
      "memo": memo or external_id,
    },
  }
  if entity_id:
    payload["entity_id"] = entity_id
  if dimension_ids:
    payload["dimension_ids"] = dimension_ids
  return emit_event(client, payload)


_FIND_EVENT = """
query($source: String!, $externalId: String!, $entityId: String) {
  eventBlocks(source: $source, externalId: $externalId, entityId: $entityId) {
    id
    status
  }
}
"""


def find_event(
  client: IntegrationClient, external_id: str, *, entity_id: str | None = None
) -> dict | None:
  """The event the platform holds for a record this source sent, as
  ``{"id", "status"}``, or None when it holds none. The integration needs
  no record of its own of what it sent.
  """
  found = client.graphql(
    _FIND_EVENT,
    {
      "source": client.config.source_name,
      "externalId": external_id,
      "entityId": entity_id,
    },
  ).get("eventBlocks")
  return found[0] if found else None


def withdraw_entry(
  client: IntegrationClient, external_id: str, *, entity_id: str | None = None
) -> dict:
  """Take an entry the source no longer holds out of the books, by the id
  the source sent it under: its event is voided and its lines leave every
  balance. A withdrawn entry cannot be sent again under the same
  ``external_id``.

  Safe to repeat: an entry already withdrawn returns
  ``{"already_withdrawn": True, ...}``, and one the platform never held
  returns ``{"not_found": True, ...}``.
  """
  event = find_event(client, external_id, entity_id=entity_id)
  if event is None:
    return {"not_found": True, "external_id": external_id}
  if event["status"] == "voided":
    return {"already_withdrawn": True, "external_id": external_id}
  return _operation(
    client,
    "update-event-block",
    {"event_id": event["id"], "transition_to": "voided", "source_retracted": True},
  )


def report_sync(
  client: IntegrationClient,
  connection_id: str,
  *,
  succeeded: bool = True,
  counts: dict[str, int] | None = None,
  message: str | None = None,
) -> dict:
  """Tell the platform a run finished. A success is the connection's
  ``last_sync``, which a period close reads to tell whether the mirror is
  current; a failure is recorded and leaves it. ``counts`` are yours to
  name (``entries_sent``, ``entries_replaced``, ...).
  """
  body: dict[str, Any] = {"status": "succeeded" if succeeded else "failed"}
  if counts:
    body["counts"] = counts
  if message:
    body["message"] = message
  return client.raw_operation(
    f"/v1/graphs/{client.config.graph_id}/connections/{connection_id}/sync-report",
    body,
  )


def _operation(client: IntegrationClient, name: str, body: dict[str, Any]) -> dict:
  return client.raw_operation(
    f"/extensions/roboledger/{client.config.graph_id}/operations/{name}", body
  )
