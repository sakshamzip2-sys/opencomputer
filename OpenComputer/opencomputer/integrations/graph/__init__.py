"""Microsoft Graph integration package.

The first *code-bearing* member of ``opencomputer/integrations/`` (the rest
are docker-compose service templates). Houses the async :class:`GraphClient`
HTTP library that wraps the Microsoft Graph ``v1.0`` REST API — mail send,
calendar listing, and OneDrive file listing.

Token acquisition / refresh (the device-code OAuth flow) is intentionally
*not* part of this package — :class:`GraphClient` takes an already-minted
access token as a plain ``str``. The OAuth surface lands separately.

Public API::

    from opencomputer.integrations.graph.client import GraphClient, GraphError
"""

from __future__ import annotations

from opencomputer.integrations.graph.client import GraphAPIError, GraphClient, GraphError

__all__ = ["GraphAPIError", "GraphClient", "GraphError"]
