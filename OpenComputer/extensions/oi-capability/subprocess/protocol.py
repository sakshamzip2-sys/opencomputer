"""JSON-RPC 2.0 protocol schemas for the OI subprocess wire protocol.

Frozen + slots dataclasses for zero-copy serialisation. Error codes per
design §7: standard JSON-RPC codes plus app-specific -32000 range.

Wire format (newline-delimited JSON):
  Parent → subprocess stdin:  JSONRPCRequest  serialised as one JSON line
  Subprocess → parent stdout: JSONRPCResponse serialised as one JSON line
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Standard JSON-RPC 2.0 error codes
# ---------------------------------------------------------------------------

class ErrorCode:
    """JSON-RPC 2.0 error codes (standard + app-specific)."""

    # Standard JSON-RPC 2.0
    PARSE_ERROR = -32700       # Invalid JSON received
    INVALID_REQUEST = -32600   # Not a valid Request object
    METHOD_NOT_FOUND = -32601  # Method does not exist
    INVALID_PARAMS = -32602    # Invalid method parameters
    INTERNAL_ERROR = -32603    # Internal JSON-RPC error

    # App-specific (-32000 to -32099)
    CONSENT_DENIED = -32000    # User did not grant consent for this action
    SANDBOX_VIOLATION = -32001 # Action blocked by sandbox policy
    TIMEOUT = -32002           # Subprocess call timed out
    TOOL_NOT_FOUND = -32003    # Requested tool/method not registered in dispatcher

    _MESSAGES: dict[int, str] = {
        -32700: "Parse error",
        -32600: "Invalid Request",
        -32601: "Method not found",
        -32602: "Invalid params",
        -32603: "Internal error",
        -32000: "Consent denied",
        -32001: "Sandbox violation",
        -32002: "Timeout",
        -32003: "Tool not found",
    }

    @classmethod
    def message(cls, code: int) -> str:
        return cls._MESSAGES.get(code, "Unknown error")


# ---------------------------------------------------------------------------
# Request / Response / Error dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class JSONRPCRequest:
    """A JSON-RPC 2.0 request (parent → subprocess)."""

    method: str
    id: int
    params: dict[str, Any] = field(default_factory=dict)
    jsonrpc: str = "2.0"

    def to_json(self) -> str:
        return json.dumps({
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
            "params": self.params,
        })

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JSONRPCRequest:
        if d.get("jsonrpc") != "2.0":
            raise ValueError("Missing or invalid jsonrpc field")
        return cls(
            method=d["method"],
            id=d["id"],
            params=d.get("params") or {},
        )

    @classmethod
    def from_json(cls, line: str) -> JSONRPCRequest:
        try:
            return cls.from_dict(json.loads(line))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ProtocolError(ErrorCode.PARSE_ERROR, str(exc)) from exc


@dataclass(frozen=True, slots=True)
class JSONRPCError:
    """A JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JSONRPCError:
        return cls(
            code=d["code"],
            message=d.get("message", ErrorCode.message(d["code"])),
            data=d.get("data"),
        )

    @classmethod
    def for_code(cls, code: int, data: Any = None) -> JSONRPCError:
        return cls(code=code, message=ErrorCode.message(code), data=data)


@dataclass(frozen=True, slots=True)
class JSONRPCResponse:
    """A JSON-RPC 2.0 response (subprocess → parent)."""

    id: int
    result: Any = None
    error: JSONRPCError | None = None
    jsonrpc: str = "2.0"

    def to_json(self) -> str:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            d["error"] = self.error.to_dict()
        else:
            d["result"] = self.result
        return json.dumps(d)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JSONRPCResponse:
        error = None
        if "error" in d:
            error = JSONRPCError.from_dict(d["error"])
        return cls(
            id=d["id"],
            result=d.get("result"),
            error=error,
        )

    @classmethod
    def from_json(cls, line: str) -> JSONRPCResponse:
        try:
            return cls.from_dict(json.loads(line))
        except (json.JSONDecodeError, KeyError) as exc:
            raise ProtocolError(ErrorCode.PARSE_ERROR, str(exc)) from exc

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def is_success(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Protocol exception
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    """Raised when the wire protocol is violated."""

    def __init__(self, code: int, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"ProtocolError({code}): {detail}")

    def to_response(self, request_id: int = 0) -> JSONRPCResponse:
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(
                code=self.code,
                message=ErrorCode.message(self.code),
                data=self.detail or None,
            ),
        )


__all__ = [
    "ErrorCode",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCError",
    "ProtocolError",
]
