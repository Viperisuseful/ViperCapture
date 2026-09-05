from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from vipercapture.render_contract import RenderRequest
from vipercapture.render_engine import (
    RenderArtifact,
    RenderLimits,
    apply_network_timing,
    collect_network_event,
    diagnostic_bundle,
    diagnostic_url,
    har_http_version,
    har_timings,
    mime_type_from_headers,
    public_network_events,
    safe_har_headers,
    _har_document,
)


def _event(**overrides: object) -> dict[str, object]:
    event = {
        "timestamp": "2026-01-01T00:00:00.000Z",
        "method": "GET",
        "url": "http://127.0.0.1/page",
        "status": 200,
        "status_text": "OK",
        "resource_type": "document",
        "http_version": "HTTP/1.1",
        "request_headers": [{"name": "Accept", "value": "text/html"}],
        "response_headers": [
            {"name": "Content-Type", "value": "text/html; charset=utf-8"},
            {"name": "Content-Length", "value": "12"},
        ],
        "mime_type": "text/html",
        "content_size": 12,
        "redirect_url": "",
        "timing": {
            "startTime": 1_700_000_000_000,
            "domainLookupStart": 0,
            "domainLookupEnd": 1,
            "connectStart": 1,
            "secureConnectionStart": -1,
            "connectEnd": 3,
            "requestStart": 3,
            "responseStart": 10,
            "responseEnd": 15,
        },
        "_request": object(),
    }
    event.update(overrides)
    return event


def test_diagnostic_url_strips_query_tokens() -> None:
    assert (
        diagnostic_url("http://127.0.0.1/callback?access_token=secret&state=1")
        == "http://127.0.0.1/callback"
    )


def test_har_http_version_prefers_protocol_and_http_cleartext() -> None:
    assert har_http_version("h2", "https://example.com") == "HTTP/2"
    assert har_http_version("http/1.1", "https://example.com") == "HTTP/1.1"
    assert har_http_version(None, "http://127.0.0.1/page") == "HTTP/1.1"
    assert har_http_version(None, "https://example.com") == "unknown"
    assert har_http_version(
        None,
        "https://example.com",
        [{"name": ":status", "value": "200"}],
    ) == "HTTP/2"


def test_safe_har_headers_redact_credentials_and_url_tokens() -> None:
    headers = safe_har_headers(
        [
            {"name": "Authorization", "value": "Bearer secret-token"},
            {"name": "Cookie", "value": "sid=abc"},
            {"name": "Accept", "value": "text/html"},
            {"name": "Location", "value": "https://example.com/next?token=secret"},
            {"name": ":status", "value": "302"},
        ]
    )
    by_name = {item["name"].lower(): item["value"] for item in headers}
    assert by_name["authorization"] == "[redacted]"
    assert by_name["cookie"] == "[redacted]"
    assert by_name["accept"] == "text/html"
    assert by_name["location"] == "https://example.com/next"
    assert ":status" not in by_name


def test_har_document_uses_observed_fields_instead_of_stubs() -> None:
    document = json.loads(_har_document([_event()]))
    entry = document["log"]["entries"][0]
    assert document["log"]["version"] == "1.2"
    assert "query strings" in document["log"]["comment"].lower()
    assert entry["request"]["httpVersion"] == "HTTP/1.1"
    assert entry["response"]["httpVersion"] == "HTTP/1.1"
    assert entry["request"]["headers"] == [{"name": "Accept", "value": "text/html"}]
    assert entry["response"]["headers"][0]["name"] == "Content-Type"
    assert entry["response"]["content"]["mimeType"] == "text/html"
    assert entry["response"]["content"]["size"] == 12
    assert entry["time"] > 0
    assert entry["timings"]["wait"] == 7
    assert entry["timings"]["receive"] == 5
    assert entry["request"]["queryString"] == []
    assert entry["request"]["cookies"] == []


def test_har_document_does_not_hardcode_http2_or_empty_headers() -> None:
    document = json.loads(
        _har_document(
            [
                _event(
                    url="http://127.0.0.1/page?access_token=secret",
                    http_version="HTTP/1.1",
                )
            ]
        )
    )
    entry = document["log"]["entries"][0]
    assert entry["request"]["httpVersion"] != "HTTP/2"
    assert entry["request"]["headers"] != []
    assert entry["time"] != 0
    assert entry["response"]["content"]["mimeType"] != "application/octet-stream"


def test_public_network_events_drop_internal_keys() -> None:
    published = public_network_events([_event()])
    assert "_request" not in published[0]
    assert published[0]["http_version"] == "HTTP/1.1"


def test_collect_network_event_redacts_query_and_keeps_safe_headers() -> None:
    request = SimpleNamespace(
        method="GET",
        resource_type="document",
        headers_array=[
            {"name": "Authorization", "value": "Bearer secret"},
            {"name": "Accept", "value": "text/html"},
        ],
        timing={
            "startTime": 1_700_000_000_000,
            "domainLookupStart": 0,
            "domainLookupEnd": 0,
            "connectStart": 0,
            "secureConnectionStart": -1,
            "connectEnd": 1,
            "requestStart": 1,
            "responseStart": 4,
            "responseEnd": 8,
        },
    )
    response = SimpleNamespace(
        url="http://127.0.0.1/page?access_token=secret",
        status=200,
        status_text="OK",
        headers={"content-type": "text/html; charset=utf-8", "content-length": "19"},
        headers_array=[
            {"name": "Content-Type", "value": "text/html; charset=utf-8"},
            {"name": "Content-Length", "value": "19"},
        ],
        request=request,
    )
    event = collect_network_event(response, http_version="http/1.1")
    assert event["url"] == "http://127.0.0.1/page"
    assert "secret" not in event["url"]
    assert event["http_version"] == "HTTP/1.1"
    assert event["mime_type"] == "text/html"
    assert event["content_size"] == 19
    assert event["status_text"] == "OK"
    assert {"name": "Accept", "value": "text/html"} in event["request_headers"]
    assert any(
        header["name"] == "Authorization" and header["value"] == "[redacted]"
        for header in event["request_headers"]
    )
    total, timings = har_timings(event["timing"])
    assert total > 0
    assert timings["wait"] == 3


def test_collect_network_event_infers_http11_for_cleartext() -> None:
    request = SimpleNamespace(
        method="GET",
        resource_type="document",
        headers_array=[],
        timing={},
    )
    response = SimpleNamespace(
        url="http://127.0.0.1/fixture",
        status=200,
        status_text="OK",
        headers={"content-type": "text/plain"},
        headers_array=[{"name": "Content-Type", "value": "text/plain"}],
        request=request,
    )
    event = collect_network_event(response)
    assert event["http_version"] == "HTTP/1.1"
    assert mime_type_from_headers(response.headers) == "text/plain"


def test_apply_network_timing_updates_receive() -> None:
    event = _event(timing={"responseStart": 10, "responseEnd": -1})
    apply_network_timing(
        event,
        {"responseStart": 10, "responseEnd": 40, "requestStart": 2},
    )
    total, timings = har_timings(event["timing"])
    assert timings["receive"] == 30
    assert total > 0


def test_diagnostic_bundle_manifest_and_har_match_privacy() -> None:
    artifact = RenderArtifact(
        b"png-bytes",
        "image/png",
        "vipercapture.png",
        {"final_url": "http://127.0.0.1/page?access_token=secret"},
    )
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "diagnostics": {
                "bundle": True,
                "include_har": True,
                "include_console": False,
                "include_network": True,
            },
        }
    )
    result = asyncio.run(
        diagnostic_bundle(
            artifact,
            request,
            [],
            [_event(url="http://127.0.0.1/page?access_token=secret")],
            RenderLimits(),
        )
    )
    with zipfile.ZipFile(io.BytesIO(result.body)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        har = json.loads(archive.read("network.har"))
        network = json.loads(archive.read("network.json"))
    assert "access_token" not in manifest["artifact"]["metadata"]["final_url"]
    assert "Safe request and response headers" in manifest["privacy"]
    entry = har["log"]["entries"][0]
    assert entry["request"]["httpVersion"] == "HTTP/1.1"
    assert entry["request"]["headers"]
    assert entry["time"] > 0
    assert entry["response"]["content"]["mimeType"] == "text/html"
    assert "secret" not in json.dumps(har)
    assert network[0]["http_version"] == "HTTP/1.1"
    assert "_request" not in network[0]


class _Http11Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = b"<!doctype html><title>fixture</title><h1>ready</h1>"
        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Fixture", "http11")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_http11_fixture_render_emits_accurate_har() -> None:
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    from vipercapture.render_engine import RenderEngine

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Http11Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    request = RenderRequest.model_validate(
        {
            "url": f"http://{host}:{port}/page?access_token=secret",
            "output": "png",
            "full_page": False,
            "viewport": {"width": 320, "height": 240},
            "stealth": False,
            "diagnostics": {
                "bundle": True,
                "include_har": True,
                "include_console": False,
                "include_network": True,
            },
        }
    )

    async def render() -> bytes:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Chromium is unavailable: {exc}")
            try:
                artifact = await RenderEngine(hosted=False).render(
                    browser, request, RenderLimits()
                )
            finally:
                await browser.close()
        return artifact.body

    try:
        body = asyncio.run(render())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        har = json.loads(archive.read("network.har"))
    serialized = json.dumps(har)
    assert "secret" not in serialized
    documents = [
        entry
        for entry in har["log"]["entries"]
        if entry["request"]["url"].endswith("/page")
    ]
    assert documents
    entry = documents[0]
    assert entry["request"]["httpVersion"] == "HTTP/1.1"
    assert entry["response"]["httpVersion"] == "HTTP/1.1"
    assert entry["request"]["headers"]
    assert entry["response"]["headers"]
    assert any(
        header["name"].lower() == "content-type"
        and "text/html" in header["value"]
        for header in entry["response"]["headers"]
    )
    assert entry["response"]["content"]["mimeType"] == "text/html"
    assert entry["time"] > 0
    assert entry["timings"]["wait"] >= 0
