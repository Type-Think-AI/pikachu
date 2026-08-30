# HANDOFF-I — MCP dependency

`src/pikachu/mcp/` needs the Python MCP SDK, but `pyproject.toml` is reserved. Integrator:
add the dependency below.

## Exact change

Add to `[project.optional-dependencies]` in `pyproject.toml` (an **extra**, so an agent with
no MCP servers never installs it — matching the lazy-import rule; the code imports `mcp` only
inside the real transport adapter, which is out of this lane):

```toml
[project.optional-dependencies]
mcp = [
    "mcp==2.1.1",
]
```

If an `mcp` extra already exists, just ensure it pins `mcp==2.1.1`.

## Why this exact version

Verified by direct inspection (recorded in `docs/22-phase0-verification.md`, Q1 RESOLVED):

```
mcp.types.LATEST_PROTOCOL_VERSION:     2026-07-28
mcp.types.DEFAULT_NEGOTIATED_VERSION:  2025-03-26
```

`2.1.1` **supports** 2026-07-28, so nothing is blocked. The `DEFAULT_NEGOTIATED_VERSION`
being three revisions behind is the trap this lane is built around: the client in
`mcp/client.py` requests `2026-07-28` explicitly (`REQUESTED_PROTOCOL_VERSION`) and asserts
the negotiated revision in a test, rather than trusting the SDK default.

Do **not** downgrade `mcp` or add `fastmcp`/`fastmcp-slim` for this lane — the thin client
here talks to the SDK through the `MCPTransport` seam and does not require FastMCP. (If a
later lane wants the tasks extension `io.modelcontextprotocol/tasks`, that pulls
`fastmcp-slim>=4.0.0b1` and is a separate decision.)

## What works without this dependency

`tests/test_mcp.py` runs **green with `mcp` not installed** — every test uses a scripted fake
transport, no network, no SDK. One optional test
(`test_sdk_default_is_actually_behind_when_installed`) uses `pytest.importorskip("mcp.types")`
and simply **skips** when the package is absent; once the dependency above is installed it
runs and proves the SDK constants match what the client assumes.

The `mcp` SDK is imported **lazily** (inside the real transport adapter, not at module scope),
so `import pikachu.mcp` — and `import pikachu` — do not pull it in.
