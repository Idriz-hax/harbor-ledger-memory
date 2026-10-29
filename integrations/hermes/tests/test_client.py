import json
from urllib.error import HTTPError, URLError

import pytest
from hlm.client import HLMClient, HLMError


def _patch_open(monkeypatch, open_request):
    class Opener:
        def open(self, request, timeout):
            return open_request(request, timeout)

    monkeypatch.setattr("hlm.client.build_opener", lambda *_handlers: Opener())


def test_requests_use_bearer_and_expected_routes(monkeypatch):
    requests = []

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size=-1):
            return json.dumps(self.payload).encode()

    def open_request(request, timeout):
        requests.append((request, timeout))
        path = request.full_url
        if "/queries" in path:
            body = {
                "selected_memories": [{"path": "A.md", "title": "A", "excerpt": "x"}]
            }
        elif "/notes/content" in path:
            body = {"path": "A.md", "content": "full", "revision": "abc"}
        elif request.get_method() == "POST":
            body = {
                "id": 1,
                "path": "A.md",
                "operation": "create",
                "status": "pending",
                "requested_at": "now",
            }
        else:
            body = {
                "proposals": [
                    {
                        "id": 1,
                        "path": "A.md",
                        "operation": "create",
                        "status": "pending",
                        "requested_at": "now",
                    }
                ],
                "next_before_id": 1,
            }
        return Response(body)

    _patch_open(monkeypatch, open_request)
    client = HLMClient("http://localhost:8765", "secret")
    client.query("find it")
    client.get_note("A.md")
    client.propose_write("A.md", "body")
    client.propose_folder("Folder")
    client.pending_proposals(limit=3, before_id=2)

    assert [r.full_url for r, _ in requests] == [
        "http://localhost:8765/api/v1/queries",
        "http://localhost:8765/api/v1/notes/content?path=A.md",
        "http://localhost:8765/api/v1/agent/writes",
        "http://localhost:8765/api/v1/agent/writes",
        "http://localhost:8765/api/v1/agent/writes/pending?limit=3&before_id=2",
    ]
    assert [r.get_method() for r, _ in requests] == [
        "POST",
        "GET",
        "POST",
        "POST",
        "GET",
    ]
    assert all(r.get_header("Authorization") == "Bearer secret" for r, _ in requests)
    assert json.loads(requests[0][0].data) == {"query": "find it"}
    assert json.loads(requests[2][0].data) == {
        "path": "A.md",
        "content": "body",
        "operation": "write",
    }
    assert json.loads(requests[3][0].data) == {
        "path": "Folder",
        "content": "",
        "operation": "mkdir",
    }
    assert json.loads(requests[2][0].data)["operation"] == "write"
    assert "api/v1/writes" not in " ".join(r.full_url for r, _ in requests)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/queries", {"selected_memories": [{}]}),
        (
            "GET",
            "/api/v1/notes/content?path=A.md",
            {"path": "A.md", "content": 1, "revision": "r"},
        ),
        ("POST", "/api/v1/agent/writes", {"id": "p"}),
        ("GET", "/api/v1/agent/writes/pending", {"proposals": {}}),
    ],
)
def test_invalid_payload_shape_raises(monkeypatch, method, path, body):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size=-1):
            return json.dumps(body).encode()

    _patch_open(monkeypatch, lambda *_args, **_kwargs: Response())
    client = HLMClient("http://localhost", "secret")
    with pytest.raises(HLMError):
        if path.endswith("queries"):
            client.query("q")
        elif "notes/content" in path:
            client.get_note("A.md")
        elif method == "POST":
            client.propose_write("A.md", "body")
        else:
            client.pending_proposals()


@pytest.mark.parametrize("proposal_id", ["1", 0, -1, True])
def test_proposal_rejects_invalid_id(proposal_id):
    with pytest.raises(HLMError):
        HLMClient._validate_proposal(
            {
                "id": proposal_id,
                "path": "A.md",
                "operation": "create",
                "status": "pending",
                "requested_at": "now",
            }
        )


@pytest.mark.parametrize("operation", ["write", "delete"])
def test_proposal_rejects_request_or_unknown_operation(operation):
    with pytest.raises(HLMError):
        HLMClient._validate_proposal(
            {
                "id": 1,
                "path": "A.md",
                "operation": operation,
                "status": "pending",
                "requested_at": "now",
            }
        )


@pytest.mark.parametrize("operation", ["create", "update", "mkdir"])
def test_proposal_accepts_actual_server_operations(operation):
    HLMClient._validate_proposal(
        {
            "id": 1,
            "path": "A.md",
            "operation": operation,
            "status": "pending",
            "requested_at": "now",
        }
    )


@pytest.mark.parametrize("status", [401, 404, 413, 422])
def test_http_errors_do_not_expose_body_or_token(monkeypatch, status):
    def fail(*_args, **_kwargs):
        raise HTTPError("http://localhost", status, "bad", {}, None)

    _patch_open(monkeypatch, fail)
    with pytest.raises(HLMError) as error:
        HLMClient("http://localhost", "secret-token").query("q")
    assert str(status) in str(error.value)
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), URLError("offline")])
def test_network_failures_raise(monkeypatch, failure):
    def fail(*_args, **_kwargs):
        raise failure

    _patch_open(monkeypatch, fail)
    with pytest.raises(HLMError):
        HLMClient("http://localhost", "secret").query("x")


def test_json_api_errors_raise_without_exposing_token(monkeypatch):
    def fail(*_args, **_kwargs):
        raise HTTPError("http://localhost", 401, "denied", {}, None)

    _patch_open(monkeypatch, fail)
    with pytest.raises(HLMError) as error:
        HLMClient("http://localhost", "do-not-leak").query("x")
    assert "do-not-leak" not in str(error.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://[::1]:8765",
        "https://memory.example",
    ],
)
def test_accepts_loopback_http_and_remote_https(base_url):
    assert HLMClient(base_url, "token").base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://memory.example",
        "https://user:password@memory.example",
        "http://user@localhost",
        "https://memory.example/path?secret=1",
        "https://memory.example/path#fragment",
    ],
)
def test_rejects_insecure_or_ambiguous_base_urls(base_url):
    with pytest.raises(ValueError):
        HLMClient(base_url, "token")


def test_redirects_are_refused_and_reported_as_hlm_error(monkeypatch):
    from urllib.request import HTTPRedirectHandler

    from hlm import client

    opened = []

    class RedirectResponse:
        def open(self, request, timeout):
            opened.append(request)
            raise HTTPError(request.full_url, 302, "redirect", {}, None)

    monkeypatch.setattr(client, "build_opener", lambda handler: RedirectResponse())
    with pytest.raises(HLMError, match="HTTP 302"):
        HLMClient("https://memory.example", "token").query("q")
    assert len(opened) == 1
    assert opened[0].get_header("Authorization") == "Bearer token"
    # Verify the installed handler opts out of urllib's redirect-following path.
    handler = client._NoRedirect()
    assert isinstance(handler, HTTPRedirectHandler)
    assert handler.redirect_request(None, None, 302, "redirect", None, None) is None


@pytest.mark.parametrize("path,content", [("x" * 4097, ""), ("x", "é" * 32769)])
def test_proposal_utf8_field_limits(path, content, monkeypatch):
    _patch_open(monkeypatch, lambda *_args: pytest.fail("request must not be sent"))
    with pytest.raises(HLMError):
        HLMClient("http://localhost", "t").propose_write(path, content)


def test_proposal_multibyte_exact_content_boundary(monkeypatch):
    _patch_open(monkeypatch, lambda *_args: pytest.fail("request must not be sent"))
    with pytest.raises(HLMError):
        HLMClient("http://localhost", "t").propose_write("x", "é" * 32769)


def test_response_is_read_only_to_cap_plus_one(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size=-1):
            assert size == 8 * 1024 * 1024 + 1
            return b" " * size

    _patch_open(monkeypatch, lambda *_args: Response())
    with pytest.raises(HLMError, match="response exceeds"):
        HLMClient("http://localhost", "t").query("q")


def test_batch_ndjson_preserves_order_failures_and_large_content(monkeypatch):
    content = "x" * (1024 * 1024 + 1)
    lines = [
        json.dumps({"path": "A.md", "ok": True, "content": content, "revision": "r"}),
        json.dumps({"path": "missing.md", "ok": False, "error": "unavailable"}),
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def __iter__(self):
            for line in lines:
                yield line.encode() + b"\n"

        def read(self, size=-1):
            pytest.fail("NDJSON response must be consumed incrementally")

    requests = []
    _patch_open(
        monkeypatch,
        lambda request, timeout: requests.append(request) or Response(),
    )
    result = HLMClient("http://localhost", "secret").get_notes_batch(
        ["A.md", "missing.md"]
    )
    assert [item["path"] for item in result] == ["A.md", "missing.md"]
    assert result[0]["content"] == content
    assert result[1]["ok"] is False
    assert requests[0].full_url == "http://localhost/api/v1/notes/content/batch"
    assert requests[0].get_method() == "POST"
    assert json.loads(requests[0].data) == {"paths": ["A.md", "missing.md"]}


def test_batch_more_than_ten_rejected_before_request(monkeypatch):
    _patch_open(monkeypatch, lambda *_args: pytest.fail("request must not be sent"))
    with pytest.raises(HLMError):
        HLMClient("http://localhost", "t").get_notes_batch(["x.md"] * 11)


def test_batch_malformed_ndjson_raises_hlm_error(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def __iter__(self):
            return iter([b"{invalid}\n"])

    _patch_open(monkeypatch, lambda *_args: Response())
    with pytest.raises(HLMError, match="invalid NDJSON"):
        HLMClient("http://localhost", "t").get_notes_batch(["x.md"])


def test_single_note_content_over_8mib_is_not_capped(monkeypatch):
    content = "x" * (8 * 1024 * 1024 + 1)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size=-1):
            assert size == -1
            return json.dumps(
                {"path": "large.md", "content": content, "revision": "r"}
            ).encode()

    _patch_open(monkeypatch, lambda *_args: Response())
    result = HLMClient("http://localhost", "t").get_note("large.md")
    assert result["content"] == content
