"""Smoke tests for the integration client + emitters (mocked HTTP)."""

from __future__ import annotations

import json

import httpx
import pytest

from integration.client import IntegrationAPIError, IntegrationClient
from integration.config import Config
from integration.emit.events import emit_event, emit_events


def _config() -> Config:
  return Config(
    api_url="https://api.test",
    api_key="key-123",
    graph_id="kg_test",
    source_name="testsource",
  )


def _envelope(result: dict | None = None) -> dict:
  """A minimal but shape-complete operation envelope (camelCase wire form)."""
  return {
    "operation": "test-op",
    "operationId": "op_1",
    "status": "completed",
    "at": "2026-07-30T00:00:00Z",
    "error": None,
    "result": result or {},
  }


def _client_with(handler) -> IntegrationClient:
  """Inject an httpx.MockTransport under the SDK client.

  ``set_httpx_client`` replaces headers too, so the auth header is
  re-supplied to mirror what the SDK builds by default.
  """
  client = IntegrationClient(_config())
  client.sdk.set_httpx_client(
    httpx.Client(
      base_url="https://api.test",
      headers={"X-API-Key": "key-123"},
      transport=httpx.MockTransport(handler),
    )
  )
  return client


class TestClient:
  def test_sdk_client_carries_api_key_header(self) -> None:
    client = IntegrationClient(_config())
    headers = client.sdk.get_httpx_client().headers
    assert headers["X-API-Key"] == "key-123"
    assert "Authorization" not in headers

  def test_raw_operation_sends_idempotency_key_and_unwraps(self) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
      seen["idem"] = request.headers.get("Idempotency-Key")
      seen["path"] = request.url.path
      return httpx.Response(200, json={"error": None, "result": {"ok": True}})

    client = _client_with(handler)
    envelope = client.raw_operation(
      "/v1/graphs/kg_test/operations/x", {}, idempotency_key="k1"
    )

    assert seen["idem"] == "k1"
    assert envelope["result"] == {"ok": True}

  def test_raw_operation_envelope_error_raises(self) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
      return httpx.Response(200, json={"error": "boom", "result": None})

    client = _client_with(handler)
    with pytest.raises(IntegrationAPIError, match="boom"):
      client.raw_operation("/v1/graphs/kg_test/operations/x", {})

  def test_raw_operation_http_error_raises_with_body(self) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
      return httpx.Response(422, json={"detail": "bad source"})

    client = _client_with(handler)
    with pytest.raises(IntegrationAPIError, match="bad source"):
      client.raw_operation("/v1/graphs/kg_test/operations/x", {})


class TestEventsEmitter:
  def test_emit_event_stamps_source_and_requires_external_id(self) -> None:
    from integration.emit.events import emit_event

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
      seen["body"] = json.loads(request.content)
      seen["path"] = request.url.path
      return httpx.Response(200, json=_envelope())

    client = _client_with(handler)
    emit_event(
      client,
      {
        "event_type": "invoice_issued",
        "event_category": "sales",
        "occurred_at": "2026-07-15T12:00:00Z",
        "external_id": "inv-1",
      },
    )
    assert seen["body"]["source"] == "testsource"
    assert seen["path"] == (
      "/extensions/roboledger/kg_test/operations/create-event-block"
    )

    with pytest.raises(ValueError, match="external_id"):
      emit_event(
        client,
        {
          "event_type": "invoice_issued",
          "event_category": "sales",
          "occurred_at": "2026-07-15T12:00:00Z",
        },
      )


class TestMetricsEmitter:
  def test_assert_metrics_builds_observation_payload(self) -> None:
    from datetime import date

    from integration.emit.metrics import assert_metrics

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
      seen["body"] = json.loads(request.content)
      seen["path"] = request.url.path
      return httpx.Response(200, json=_envelope())

    client = _client_with(handler)
    assert_metrics(
      client,
      structure_id="str_growth",
      period_end=date(2026, 7, 31),
      period_start=date(2026, 7, 1),
      observations={"rsx:GithubStars": 1240.0},
    )

    assert seen["path"].endswith("/operations/assert-metrics")
    assert seen["body"]["source_system"] == "testsource"
    assert seen["body"]["observations"] == [
      {"qname": "rsx:GithubStars", "value": 1240.0}
    ]


class TestEmitEvent:
  _PAYLOAD = {
    "event_type": "invoice_issued",
    "event_category": "sales",
    "occurred_at": "2026-07-30T00:00:00Z",
    "external_id": "inv-1",
  }
  _REPEAT = {"detail": "Event already ingested for this source and external_id"}

  def test_a_repeat_returns_already_ingested_instead_of_raising(self) -> None:
    client = _client_with(lambda _request: httpx.Response(409, json=self._REPEAT))

    assert emit_event(client, self._PAYLOAD) == {
      "already_ingested": True,
      "source": "testsource",
      "external_id": "inv-1",
    }

  def test_any_other_409_still_raises(self) -> None:
    client = _client_with(
      lambda _request: httpx.Response(409, json={"detail": "Row is locked"})
    )

    with pytest.raises(IntegrationAPIError, match="Row is locked"):
      emit_event(client, self._PAYLOAD)

  def test_a_batch_carries_on_past_a_repeat(self) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
      seen.append(json.loads(request.content)["external_id"])
      return httpx.Response(409, json=self._REPEAT)

    client = _client_with(handler)
    results = emit_events(
      client,
      [self._PAYLOAD, {**self._PAYLOAD, "external_id": "inv-2"}],
    )

    assert seen == ["inv-1", "inv-2"]
    assert all(result["already_ingested"] for result in results)


class TestLedgerEmitter:
  """A general ledger kept elsewhere, mirrored read-only."""

  @staticmethod
  def _recording(responses: dict | None = None):
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
      seen.append((request.url.path, json.loads(request.content)))
      status, body = (responses or {}).get(request.url.path, (200, _envelope()))
      return httpx.Response(status, json=body)

    return _client_with(handler), seen

  def test_register_ledger_names_the_source_and_its_entity(self) -> None:
    from integration.emit.ledger import register_ledger

    client, seen = self._recording()
    register_ledger(client, entity_id="ent_sub", display_name="ERP (UK)")
    register_ledger(client)

    assert seen[0] == (
      "/v1/graphs/kg_test/connections",
      {
        "provider": "external",
        "external_config": {
          "source_name": "testsource",
          "ledger": True,
          "entity_id": "ent_sub",
          "display_name": "ERP (UK)",
        },
      },
    )
    assert seen[1][1]["external_config"] == {
      "source_name": "testsource",
      "ledger": True,
    }

  def test_emit_journal_entry_sends_a_versioned_posted_entry(self) -> None:
    from datetime import date

    from integration.emit.ledger import emit_journal_entry

    client, seen = self._recording()
    lines = [
      {"element_id": "elem_cash", "debit_amount": 62500, "credit_amount": 0},
      {"element_id": "elem_rev", "debit_amount": 0, "credit_amount": 62500},
    ]
    emit_journal_entry(
      client,
      external_id="JE-1",
      version=2,
      posting_date=date(2026, 9, 20),
      lines=lines,
      memo="Rent received",
      entity_id="ent_sub",
    )

    path, body = seen[0]
    assert path == "/extensions/roboledger/kg_test/operations/create-event-block"
    assert body["source"] == "testsource"
    assert body["external_id"] == "JE-1"
    assert body["source_version"] == 2
    assert body["entity_id"] == "ent_sub"
    assert body["event_type"] == "journal_entry_recorded"
    assert body["apply_handlers"] is True
    assert body["amount"] == 62500
    assert body["occurred_at"].startswith("2026-09-20")
    assert body["metadata"] == {
      "posting_date": "2026-09-20",
      "status": "posted",
      "line_items": lines,
      "memo": "Rent received",
    }

  def test_a_repeat_of_the_same_version_is_already_ingested(self) -> None:
    from datetime import date

    from integration.emit.ledger import emit_journal_entry

    path = "/extensions/roboledger/kg_test/operations/create-event-block"
    client, _seen = self._recording(
      {
        path: (
          409,
          {"detail": "Event already ingested for this source and external_id"},
        )
      }
    )
    result = emit_journal_entry(
      client, external_id="JE-1", version=2, posting_date=date(2026, 9, 20), lines=[]
    )
    assert result["already_ingested"] is True
    assert _seen[0][1]["metadata"]["memo"] == "JE-1"

  def test_a_version_starts_at_one(self) -> None:
    from datetime import date

    from integration.emit.ledger import emit_journal_entry

    client, seen = self._recording()
    with pytest.raises(ValueError, match="version"):
      emit_journal_entry(
        client, external_id="JE-1", version=0, posting_date=date(2026, 9, 1), lines=[]
      )
    assert seen == []

  def test_withdraw_opening_balance_and_sync_report(self) -> None:
    from integration.emit.ledger import (
      record_opening_balance,
      report_sync,
      withdraw_entry,
    )

    client, seen = self._recording(
      {
        "/extensions/kg_test/graphql": (
          200,
          {"data": {"eventBlocks": [{"id": "evt_1", "status": "fulfilled"}]}},
        )
      }
    )
    withdraw_entry(client, "JE-2", entity_id="ent_sub")
    record_opening_balance(
      client,
      [{"element_id": "elem_cash", "debit_amount": 1000000, "credit_amount": 0}],
      entity_id="ent_sub",
      equity_element_id="elem_equity",
    )
    report_sync(client, "conn_1", counts={"entries_sent": 3})
    report_sync(client, "conn_1", succeeded=False, message="source timed out")

    assert seen[0][0] == "/extensions/kg_test/graphql"
    assert seen[0][1]["variables"] == {
      "source": "testsource",
      "externalId": "JE-2",
      "entityId": "ent_sub",
    }
    assert seen[1] == (
      "/extensions/roboledger/kg_test/operations/update-event-block",
      {"event_id": "evt_1", "transition_to": "voided", "source_retracted": True},
    )
    assert seen[2][0] == (
      "/extensions/roboledger/kg_test/operations/record-opening-balance"
    )
    assert seen[2][1]["entity_id"] == "ent_sub"
    assert seen[2][1]["equity_element_id"] == "elem_equity"
    assert seen[3] == (
      "/v1/graphs/kg_test/connections/conn_1/sync-report",
      {"status": "succeeded", "counts": {"entries_sent": 3}},
    )
    assert seen[4][1] == {"status": "failed", "message": "source timed out"}

  @pytest.mark.parametrize(
    ("held", "answer"),
    [
      ([], {"not_found": True, "external_id": "JE-2"}),
      (
        [{"id": "evt_1", "status": "voided"}],
        {"already_withdrawn": True, "external_id": "JE-2"},
      ),
    ],
  )
  def test_a_withdrawal_with_nothing_to_withdraw_changes_nothing(
    self, held, answer
  ) -> None:
    from integration.emit.ledger import withdraw_entry

    client, seen = self._recording(
      {"/extensions/kg_test/graphql": (200, {"data": {"eventBlocks": held}})}
    )
    assert withdraw_entry(client, "JE-2") == answer
    assert [path for path, _ in seen] == ["/extensions/kg_test/graphql"]

  def test_a_refused_registration_raises_with_the_reason(self) -> None:
    from integration.emit.ledger import register_ledger

    client, _seen = self._recording(
      {
        "/v1/graphs/kg_test/connections": (
          409,
          {"detail": {"code": "BOOKS_KEPT_ELSEWHERE"}},
        )
      }
    )
    with pytest.raises(IntegrationAPIError, match="BOOKS_KEPT_ELSEWHERE"):
      register_ledger(client, entity_id="ent_sub")
