"""Tests for extensions/oi-capability/subprocess/protocol.py.

Covers JSON-RPC 2.0 request/response schemas, error code mapping,
serialisation/deserialisation, and ProtocolError.
"""

from __future__ import annotations

import json

import pytest
from extensions.coding_harness.oi_bridge.subprocess.protocol import (
    ErrorCode,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    ProtocolError,
)


class TestErrorCode:
    def test_standard_codes_defined(self):
        assert ErrorCode.PARSE_ERROR == -32700
        assert ErrorCode.INVALID_REQUEST == -32600
        assert ErrorCode.METHOD_NOT_FOUND == -32601
        assert ErrorCode.INVALID_PARAMS == -32602
        assert ErrorCode.INTERNAL_ERROR == -32603

    def test_app_specific_codes_defined(self):
        assert ErrorCode.CONSENT_DENIED == -32000
        assert ErrorCode.SANDBOX_VIOLATION == -32001
        assert ErrorCode.TIMEOUT == -32002
        assert ErrorCode.TOOL_NOT_FOUND == -32003

    def test_message_for_known_codes(self):
        assert ErrorCode.message(-32700) == "Parse error"
        assert ErrorCode.message(-32601) == "Method not found"
        assert ErrorCode.message(-32000) == "Consent denied"
        assert ErrorCode.message(-32003) == "Tool not found"

    def test_message_for_unknown_code(self):
        assert ErrorCode.message(-99999) == "Unknown error"


class TestJSONRPCRequest:
    def test_round_trip(self):
        req = JSONRPCRequest(method="computer.files.search", id=42, params={"query": "hello"})
        serialised = req.to_json()
        parsed = json.loads(serialised)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == 42
        assert parsed["method"] == "computer.files.search"
        assert parsed["params"] == {"query": "hello"}

    def test_from_dict(self):
        d = {"jsonrpc": "2.0", "id": 1, "method": "computer.clipboard.view", "params": {}}
        req = JSONRPCRequest.from_dict(d)
        assert req.id == 1
        assert req.method == "computer.clipboard.view"
        assert req.params == {}

    def test_from_json(self):
        line = '{"jsonrpc": "2.0", "id": 7, "method": "computer.display.ocr", "params": {}}'
        req = JSONRPCRequest.from_json(line)
        assert req.id == 7
        assert req.method == "computer.display.ocr"

    def test_from_json_missing_jsonrpc_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            JSONRPCRequest.from_json('{"id": 1, "method": "x"}')
        assert exc_info.value.code == ErrorCode.PARSE_ERROR

    def test_from_json_invalid_json_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            JSONRPCRequest.from_json("{not valid json}")
        assert exc_info.value.code == ErrorCode.PARSE_ERROR

    def test_default_params_is_empty_dict(self):
        req = JSONRPCRequest(method="shutdown", id=0)
        assert req.params == {}
        assert req.jsonrpc == "2.0"


class TestJSONRPCError:
    def test_to_dict(self):
        err = JSONRPCError(code=-32601, message="Method not found")
        d = err.to_dict()
        assert d["code"] == -32601
        assert d["message"] == "Method not found"
        assert "data" not in d  # data is None, so excluded

    def test_to_dict_with_data(self):
        err = JSONRPCError(code=-32603, message="Internal error", data="traceback...")
        d = err.to_dict()
        assert d["data"] == "traceback..."

    def test_from_dict(self):
        d = {"code": -32000, "message": "Consent denied", "data": None}
        err = JSONRPCError.from_dict(d)
        assert err.code == -32000
        assert err.message == "Consent denied"

    def test_for_code(self):
        err = JSONRPCError.for_code(ErrorCode.TIMEOUT)
        assert err.code == -32002
        assert err.message == "Timeout"
        assert err.data is None


class TestJSONRPCResponse:
    def test_success_round_trip(self):
        resp = JSONRPCResponse(id=5, result={"files": ["a.py", "b.py"]})
        serialised = resp.to_json()
        parsed = json.loads(serialised)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == 5
        assert parsed["result"] == {"files": ["a.py", "b.py"]}
        assert "error" not in parsed

    def test_error_round_trip(self):
        err = JSONRPCError(code=-32601, message="Method not found")
        resp = JSONRPCResponse(id=3, error=err)
        serialised = resp.to_json()
        parsed = json.loads(serialised)
        assert "result" not in parsed
        assert parsed["error"]["code"] == -32601

    def test_is_error_false_on_success(self):
        resp = JSONRPCResponse(id=1, result="ok")
        assert not resp.is_error
        assert resp.is_success

    def test_is_error_true_on_error(self):
        resp = JSONRPCResponse(id=1, error=JSONRPCError.for_code(ErrorCode.INTERNAL_ERROR))
        assert resp.is_error
        assert not resp.is_success

    def test_from_json_success(self):
        line = '{"jsonrpc": "2.0", "id": 9, "result": "clipboard text"}'
        resp = JSONRPCResponse.from_json(line)
        assert resp.id == 9
        assert resp.result == "clipboard text"
        assert resp.error is None

    def test_from_json_error(self):
        line = '{"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "Internal error"}}'
        resp = JSONRPCResponse.from_json(line)
        assert resp.id == 2
        assert resp.error is not None
        assert resp.error.code == -32603


class TestProtocolError:
    def test_raises_with_code(self):
        exc = ProtocolError(ErrorCode.PARSE_ERROR, "bad json")
        assert exc.code == ErrorCode.PARSE_ERROR
        assert "bad json" in exc.detail

    def test_to_response(self):
        exc = ProtocolError(ErrorCode.METHOD_NOT_FOUND, "computer.x.y not found")
        resp = exc.to_response(request_id=42)
        assert resp.id == 42
        assert resp.error is not None
        assert resp.error.code == ErrorCode.METHOD_NOT_FOUND
