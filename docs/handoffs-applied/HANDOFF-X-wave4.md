# HANDOFF-X — expose the MCP server at the package boundary

Lane X (F12, MCP **server** mode) added `src/pikachu/mcp/server.py`. Everything works and is
tested by importing `pikachu.mcp.server` **directly**, so nothing below is required for the
lane to pass — this handoff is purely to make the server a first-class, lazily-imported
package export the same way the client already is.

Both files are **reserved** (integrator-only), so the lane does not edit them:

- `src/pikachu/mcp/__init__.py`
- (no `pyproject.toml` change is needed — the server has no new dependency; it reuses the
  client's constants and the guard, both already in-tree.)

## Why this is optional, not blocking

`server.py` reuses the client's protocol constants (`REQUESTED_PROTOCOL_VERSION`,
`ResultType`) and the guard (`effective_tools`). It adds no import cost beyond what the client
already pays, and it does **not** import the `mcp` SDK. Tests import
`from pikachu.mcp.server import MCPServer, ServerResult, request_more_input,
ADVERTISED_PROTOCOL_VERSION` directly and never touch `__init__`. So the lane is green without
this change.

## Exact change — `src/pikachu/mcp/__init__.py`

The module uses a PEP 562 `__getattr__` lazy re-export. Add the four server symbols to
`__all__`, to the `TYPE_CHECKING` import block, and route them to the `server` submodule in
`__getattr__` (they must resolve from `server`, not `client`).

```diff
--- a/src/pikachu/mcp/__init__.py
+++ b/src/pikachu/mcp/__init__.py
@@
 __all__ = [
     "REQUESTED_PROTOCOL_VERSION",
+    "ADVERTISED_PROTOCOL_VERSION",
     "DiscoveredTool",
     "InputRequired",
     "MCPClient",
     "MCPDiscovery",
     "MCPProtocolError",
     "MCPResult",
+    "MCPServer",
     "MCPTransport",
     "ResultType",
+    "ServerResult",
+    "ToolInvoker",
+    "request_more_input",
 ]

 if TYPE_CHECKING:
     from pikachu.mcp.client import (
         REQUESTED_PROTOCOL_VERSION,
         DiscoveredTool,
         InputRequired,
         MCPClient,
         MCPDiscovery,
         MCPProtocolError,
         MCPResult,
         MCPTransport,
         ResultType,
     )
+    from pikachu.mcp.server import (
+        ADVERTISED_PROTOCOL_VERSION,
+        MCPServer,
+        ServerResult,
+        ToolInvoker,
+        request_more_input,
+    )


 def __getattr__(name: str) -> Any:
-    if name in __all__:
+    _SERVER_NAMES = {
+        "ADVERTISED_PROTOCOL_VERSION",
+        "MCPServer",
+        "ServerResult",
+        "ToolInvoker",
+        "request_more_input",
+    }
+    if name in _SERVER_NAMES:
+        from pikachu.mcp import server
+
+        return getattr(server, name)
+    if name in __all__:
         from pikachu.mcp import client

         return getattr(client, name)
     raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

Note `REQUESTED_PROTOCOL_VERSION` and `ResultType` stay routed to `client` (server.py imports
them from there, so either origin returns the same object). Only the five server-native names
route to `server`.

## Acceptance tests — add to `tests/test_mcp_server.py` (Lane X owns this file)

These are written `@pytest.mark.xfail(strict=True)` so they PASS-as-xfail until the `__init__`
change lands, then flip to hard failures if the integrator forgets the edit or wires it wrong.
Once the diff above is applied, remove the `xfail` marks (they will `XPASS` and, being
`strict=True`, fail the suite until removed — that is the tripwire working).

```python
import importlib

import pytest


@pytest.mark.xfail(strict=True, reason="HANDOFF-X: __init__ server export not yet applied")
def test_server_exported_from_package_root() -> None:
    mcp = importlib.import_module("pikachu.mcp")
    assert mcp.MCPServer is not None
    assert mcp.ServerResult is not None
    assert mcp.ADVERTISED_PROTOCOL_VERSION == "2026-07-28"
    assert callable(mcp.request_more_input)
    for name in (
        "ADVERTISED_PROTOCOL_VERSION",
        "MCPServer",
        "ServerResult",
        "ToolInvoker",
        "request_more_input",
    ):
        assert name in mcp.__all__


@pytest.mark.xfail(strict=True, reason="HANDOFF-X: lazy import must not pull the mcp SDK")
def test_importing_mcp_package_does_not_import_sdk() -> None:
    import sys

    # Fresh import of the package alone must not drag in the optional SDK.
    for mod in [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]:
        del sys.modules[mod]
    importlib.reload(importlib.import_module("pikachu.mcp"))
    assert "mcp" not in sys.modules
```

## Verified state at handoff

```
.venv/bin/python -m pytest tests/test_mcp_server.py -q   ->  22 passed
.venv/bin/python -m mypy --strict src/pikachu/mcp/server.py  ->  Success: no issues found
```

Full-package `mypy --strict src/pikachu` reports 2 errors, both in
`src/pikachu/telemetry/otel.py` (Lane V), none in `mcp/`.
