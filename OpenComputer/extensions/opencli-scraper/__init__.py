# ruff: noqa: N999
"""OpenCLI Scraper plugin — package marker.

Wraps the OpenCLI binary (https://github.com/jackwener/opencli, Apache-2.0)
as a safe, rate-limited, robots.txt-aware scraping extension for OpenComputer.

The extension directory is named ``opencli-scraper`` (with a hyphen) following
the project convention for all extensions (e.g. ``coding-harness``). This
package is loaded via the plugin loader, not imported as a Python package.

Tools are NOT registered until Session A's Phase 4 wires in ConsentGate and
SignalNormalizer (see design doc §14).
"""

__version__ = "0.1.0"
