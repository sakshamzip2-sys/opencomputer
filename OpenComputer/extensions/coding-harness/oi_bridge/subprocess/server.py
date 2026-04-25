"""OI subprocess JSON-RPC server — runs INSIDE the OI venv.

Run as: python extensions/oi_capability/subprocess/server.py

CRITICAL IMPORT ORDER: telemetry_disable MUST be the very first import
(before any 'from interpreter' or 'import interpreter'). Do not reorder.

Reads JSON-RPC 2.0 requests from stdin (newline-delimited).
Writes JSON-RPC 2.0 responses to sys.__stdout__ (real stdout).
OI's own print() output is redirected to a BytesIO sink so it doesn't
pollute the JSON-RPC channel.
"""

from __future__ import annotations

# =========================================================================
# Line 1: TELEMETRY KILL — MUST be first; before any OI import
# =========================================================================
# ruff: noqa: E402, I001, PLC0415
import sys as _sys

# Patch sys.modules for telemetry before OI can import it
_noop_sentinel = type(
    "_NoopModule",
    (),
    {
        "send_telemetry": staticmethod(lambda *a, **k: None),
        "get_distinct_id": staticmethod(lambda: "opencomputer-noop"),
        "posthog": None,
        "__getattr__": lambda self, n: (lambda *a, **k: None),
    },
)()
_sys.modules.setdefault("interpreter.core.utils.telemetry", _noop_sentinel)  # type: ignore[assignment]

# Also disable litellm telemetry before OI pulls it in
try:
    import litellm as _litellm  # noqa: PLC0415
    _litellm.telemetry = False
    _turn_off = getattr(_litellm, "_turn_off_message_logging", None)
    if callable(_turn_off):
        _turn_off()
except ImportError:
    import os as _os  # noqa: PLC0415
    _os.environ.setdefault("LITELLM_TELEMETRY", "False")

# =========================================================================
# Redirect sys.stdout → BytesIO so OI print()s don't pollute JSON-RPC
# =========================================================================
import io as _io  # noqa: PLC0415, E402

_real_stdout = _sys.__stdout__
_sys.stdout = _io.StringIO()  # absorbs OI's incidental print() calls

# =========================================================================
# NOW safe to import Open Interpreter
# =========================================================================
import json  # noqa: E402
import logging  # noqa: E402
import traceback  # noqa: E402

try:
    from interpreter import OpenInterpreter  # noqa: E402 — ONLY allowed here
    _HAS_OI = True
except ImportError:
    _HAS_OI = False
    OpenInterpreter = None  # type: ignore[assignment, misc]

# =========================================================================
# Logging to stderr (subprocess.log gets stderr from parent wrapper)
# =========================================================================
logging.basicConfig(
    stream=_sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [oi-server] %(levelname)s %(message)s",
)
_log = logging.getLogger("oi-server")

# =========================================================================
# Build the OI interpreter instance
# =========================================================================
if _HAS_OI:
    _interpreter = OpenInterpreter()
    _interpreter.auto_run = True  # do NOT prompt user for confirmations
    _interpreter.llm.model = "openai/gpt-4o"  # default; callers can override via params
    _log.info("OpenInterpreter instance created (auto_run=True)")
else:
    _interpreter = None
    _log.warning("open-interpreter not installed — running in stub mode")

# =========================================================================
# Dispatcher: method → OI call
# =========================================================================

_SHUTDOWN_METHODS = {"shutdown", "exit", "quit"}


def _dispatch(method: str, params: dict) -> object:
    """Route a JSON-RPC method to the correct OI capability."""
    if method in _SHUTDOWN_METHODS:
        _log.info("Received shutdown request")
        _sys.exit(0)

    if not _HAS_OI or _interpreter is None:
        raise LookupError(f"open-interpreter not installed; cannot dispatch '{method}'")

    # Map "computer.X.Y" to interpreter.computer.X.Y(**params)
    if not method.startswith("computer."):
        raise LookupError(f"Unknown method '{method}' — expected 'computer.*' namespace")

    parts = method.split(".")
    # parts = ["computer", "<module>", "<method>"]
    if len(parts) < 3:
        raise LookupError(f"Method '{method}' must be at least 'computer.<module>.<method>'")

    module_name = parts[1]
    method_name = parts[2]

    module = getattr(_interpreter.computer, module_name, None)
    if module is None:
        raise LookupError(f"OI computer module '{module_name}' not found")

    fn = getattr(module, method_name, None)
    if fn is None or not callable(fn):
        raise LookupError(
            f"OI computer.{module_name}.{method_name} not found or not callable"
        )

    return fn(**params)


# =========================================================================
# JSON-RPC dispatch loop
# =========================================================================

def _make_error_response(req_id: int, code: int, message: str, data: str = "") -> dict:
    err: dict = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _make_ok_response(req_id: int, result: object) -> dict:
    try:
        # Ensure result is JSON-serialisable
        json.dumps(result)
    except (TypeError, ValueError):
        result = str(result)
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _write(obj: dict) -> None:
    line = json.dumps(obj) + "\n"
    _real_stdout.write(line)
    _real_stdout.flush()


def main() -> None:
    _log.info("OI subprocess server starting (auto_run=%s)", getattr(_interpreter, "auto_run", "N/A"))

    for raw_line in _sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        req_id = 0
        try:
            req = json.loads(raw_line)
            req_id = req.get("id", 0)
            method = req.get("method", "")
            params = req.get("params") or {}

            if req.get("jsonrpc") != "2.0":
                _write(_make_error_response(req_id, -32600, "Invalid Request", "jsonrpc field missing"))
                continue

            result = _dispatch(method, params)
            _write(_make_ok_response(req_id, result))

        except json.JSONDecodeError as exc:
            _write(_make_error_response(req_id, -32700, "Parse error", str(exc)))
        except LookupError as exc:
            _write(_make_error_response(req_id, -32601, "Method not found", str(exc)))
        except TypeError as exc:
            _write(_make_error_response(req_id, -32602, "Invalid params", str(exc)))
        except SystemExit:
            _write(_make_ok_response(req_id, {"status": "shutdown"}))
            break
        except Exception as exc:  # noqa: BLE001
            _log.error("Unhandled dispatch error: %s", exc, exc_info=True)
            _write(_make_error_response(
                req_id, -32603, "Internal error", traceback.format_exc()
            ))

    _log.info("OI subprocess server exiting")


if __name__ == "__main__":
    main()
