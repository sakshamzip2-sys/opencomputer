# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Proactive security monitoring helpers.

Composes Tier 3 and Tier 5 tools to scan for suspicious processes and
flag known-malicious browser history entries.

Note: The suspicious process / domain lists are intentionally small demo
lists. They are not exhaustive threat intelligence feeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tools.tier_3_browser import ReadBrowserHistoryTool
from ..tools.tier_5_advanced import ListRunningProcessesTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper

# Well-known suspicious process names (non-exhaustive demo list)
SUSPICIOUS_PROCESSES: frozenset[str] = frozenset({
    "keylogger",
    "spyware",
    "miner",
    "cryptominer",
    "ratd",
    "backdoor",
    "rootkit",
    "stalkerware",
    "pegasus",
    "flexispy",
})

# Known-malicious domains (non-exhaustive demo list)
SUSPICIOUS_DOMAINS: frozenset[str] = frozenset({
    "malware.test",
    "phishing.example",
    "evil.corp",
    "cryptojack.io",
    "spyware-cdn.net",
    "track-me-now.ru",
})


def _is_suspicious_process(name: str) -> bool:
    name_lower = name.lower()
    return any(s in name_lower for s in SUSPICIOUS_PROCESSES)


def _is_suspicious_url(url: str) -> bool:
    url_lower = url.lower()
    return any(domain in url_lower for domain in SUSPICIOUS_DOMAINS)


async def scan_processes(wrapper: OISubprocessWrapper) -> dict:
    """List running processes and flag any that match SUSPICIOUS_PROCESSES.

    Uses :class:`ListRunningProcessesTool` (Tier 5).

    Returns::

        {
            "total": int,
            "suspicious": [{"name": ..., "pid": ..., "reason": ...}, ...],
            "all": [{"name": ..., "pid": ...}, ...],
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ListRunningProcessesTool(wrapper=wrapper)
    call = ToolCall(
        id="scan-processes",
        name="list_running_processes",
        arguments={"limit": 200},
    )
    result = await tool.execute(call)

    all_procs: list[dict] = []
    suspicious: list[dict] = []

    if result.is_error or not result.content.strip():
        return {"total": 0, "suspicious": [], "all": []}

    # Parse ps aux output (lines after header)
    lines = result.content.strip().splitlines()
    for line in lines:
        # ps aux: USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        pid = parts[1]
        command = parts[10]
        # Extract process name (first token of command, basename)
        proc_name = command.split("/")[-1].split()[0] if command else ""

        entry = {"name": proc_name, "pid": pid, "command": command}
        all_procs.append(entry)

        if _is_suspicious_process(proc_name) or _is_suspicious_process(command):
            suspicious.append({
                "name": proc_name,
                "pid": pid,
                "reason": "matched SUSPICIOUS_PROCESSES list",
                "command": command,
            })

    return {"total": len(all_procs), "suspicious": suspicious, "all": all_procs}


async def check_recent_browser_history(
    wrapper: OISubprocessWrapper,
    *,
    hours: int = 24,
) -> list[dict]:
    """Fetch browser history and flag visits to suspicious domains.

    Uses :class:`ReadBrowserHistoryTool` (Tier 3) and filters to entries
    within the last *hours* hours, then flags any that match SUSPICIOUS_DOMAINS.

    Returns a list of::

        {
            "url": str,
            "title": str,
            "visited_at": str,  # raw timestamp from history DB
            "is_suspicious": bool,
            "matched_domain": str | None,
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ReadBrowserHistoryTool(wrapper=wrapper)
    call = ToolCall(
        id="browser-history-security",
        name="read_browser_history",
        arguments={"limit": 500, "days": max(1, hours // 24 + 1)},
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return []

    entries: list[dict] = []
    # History DB output: url|title|last_visit_time (separated by |)
    for line in result.content.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        url = parts[0].strip()
        title = parts[1].strip() if len(parts) > 1 else ""
        visited_at = parts[2].strip() if len(parts) > 2 else ""

        matched_domain = next(
            (d for d in SUSPICIOUS_DOMAINS if d in url.lower()), None
        )
        entries.append({
            "url": url,
            "title": title,
            "visited_at": visited_at,
            "is_suspicious": matched_domain is not None,
            "matched_domain": matched_domain,
        })

    return entries


async def sweep(wrapper: OISubprocessWrapper) -> dict:
    """Run all security checks and return a combined report.

    Executes :func:`scan_processes` and :func:`check_recent_browser_history`
    concurrently and merges the results.

    Returns::

        {
            "processes": {<scan_processes result>},
            "browser_history": [<check_recent_browser_history result>],
            "summary": {
                "suspicious_process_count": int,
                "suspicious_url_count": int,
                "overall_risk": "low" | "medium" | "high",
            },
        }
    """
    import asyncio

    proc_task = asyncio.create_task(scan_processes(wrapper))
    hist_task = asyncio.create_task(check_recent_browser_history(wrapper))
    proc_report, hist_report = await asyncio.gather(proc_task, hist_task)

    suspicious_procs = len(proc_report.get("suspicious", []))
    suspicious_urls = sum(1 for e in hist_report if e.get("is_suspicious"))

    if suspicious_procs > 0 or suspicious_urls > 0:
        risk = "high" if suspicious_procs > 0 else "medium"
    else:
        risk = "low"

    return {
        "processes": proc_report,
        "browser_history": hist_report,
        "summary": {
            "suspicious_process_count": suspicious_procs,
            "suspicious_url_count": suspicious_urls,
            "overall_risk": risk,
        },
    }
