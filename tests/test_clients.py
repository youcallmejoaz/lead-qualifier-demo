"""Exercise the real pyairtable and Anthropic SDK code paths with the network mocked out."""

import json

import anthropic
import httpx2 as httpx  # the transport library the anthropic 1.x SDK uses
import pytest
import requests

from src.airtable_client import AirtableClient
from src.models import Assessment
from src.qualifier import Qualifier, QualifierError, TOOL_NAME
from src.sync import http_status
from tests.fakes import ASSESSMENTS


def _airtable_with(status: int, body: dict):
    client = AirtableClient("patTEST", "appTEST")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        r.url = url
        r.request = requests.Request(method, url).prepare()
        return r

    client.api.session.request = fake_request
    return client, calls


def test_upsert_is_one_patch_request_merging_on_account_id():
    record = {"id": "rec1", "createdTime": "2026-10-14T00:00:00.000Z", "fields": {"Account ID": "a1", "Handle": "x"}}
    client, calls = _airtable_with(200, {"records": [record], "createdRecords": ["rec1"], "updatedRecords": []})
    out = client.upsert_leads([{"Account ID": "a1", "Handle": "x"}])
    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "patch" and url.endswith("/appTEST/Leads")
    assert body["performUpsert"] == {"fieldsToMergeOn": ["Account ID"]}
    assert out["records"][0]["id"] == "rec1"


def test_429_reaches_our_worker_without_hidden_retries():
    client, calls = _airtable_with(429, {"errors": [{"error": "RATE_LIMIT_REACHED"}]})
    with pytest.raises(requests.HTTPError) as exc:
        client.upsert_leads([{"Account ID": "a1"}])
    assert http_status(exc.value) == 429
    assert len(calls) == 1  # pyairtable's own retry is off; SyncWorker owns the 30 s wait


def test_more_than_10_records_is_refused():
    client, _ = _airtable_with(200, {})
    with pytest.raises(AssertionError):
        client.upsert_leads([{"Account ID": str(i)} for i in range(11)])


def _claude_returning(content: list, stop_reason="tool_use"):
    seen = []

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return client, seen


def test_qualifier_request_shape_and_parsing(rubric, load_account):
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": TOOL_NAME, "input": ASSESSMENTS["sample_qualified"]}
    client, seen = _claude_returning([tool_use])
    result = Qualifier(client, "claude-sonnet-5-5", rubric).assess(load_account("sample_qualified"))
    assert isinstance(result, Assessment) and result.offer_works.score == 9

    body = seen[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "auto"}  # forced tool_choice is rejected by current models
    assert body["tools"][0]["strict"] is True
    assert body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Manual hand raiser loop" in body["system"][0]["text"]
    assert 'handle="@sample_qualified"' in body["messages"][0]["content"]


def test_qualifier_retries_once_when_no_tool_call_then_fails(rubric, load_account):
    client, seen = _claude_returning([{"type": "text", "text": "Here is my view..."}], stop_reason="end_turn")
    with pytest.raises(QualifierError):
        Qualifier(client, "claude-sonnet-5-5", rubric).assess(load_account("sample_qualified"))
    assert len(seen) == 2


def test_qualifier_rejects_out_of_range_scores(rubric, load_account):
    bad = json.loads(json.dumps(ASSESSMENTS["sample_qualified"]))
    bad["offer_works"]["score"] = 42
    client, seen = _claude_returning([{"type": "tool_use", "id": "t1", "name": TOOL_NAME, "input": bad}])
    with pytest.raises(QualifierError, match="less than or equal to 10"):
        Qualifier(client, "claude-sonnet-5-5", rubric).assess(load_account("sample_qualified"))
    # the retry sent the validation error back as a tool_result
    assert seen[1]["messages"][-1]["content"][0]["type"] == "tool_result"
