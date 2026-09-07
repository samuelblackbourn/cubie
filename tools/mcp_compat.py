"""Opening an MCP session across two incompatible SDK generations.

The Python MCP SDK reshaped its streamable-HTTP transport, and this repo's
scripts have to run against whichever version the gateway's virtualenv happens
to pin:

    1.12:  streamablehttp_client(url, headers=...)         yields 3 streams
    1.30:  both names present; streamable_http_client()    yields 3 streams
    2.2:   streamable_http_client(url, http_client=...)    yields 2 streams

The rename fails loudly at import, which is the easy half. The dangerous change
is `CallToolResult.isError` becoming `is_error`: a `getattr(result, "isError",
False)` reads as correct, imports fine on every version, and silently returns
False for every failing tool call. A script written that way reports success on
failure.

This module exists so that reasoning lives in one place. It was duplicated
across two scripts, which is two chances to drift and one of them was already
wrong.

Import it with the GATEWAY'S interpreter -- the `mcp` package is in the
gateway's virtualenv, not the system python.
"""

import contextlib

try:
    import mcp.client.streamable_http as transport_module
    from mcp import ClientSession as ClientSession  # re-exported for callers
except ImportError as exc:  # pragma: no cover - depends on the interpreter used
    raise ImportError(
        "the `mcp` package is not importable. Run this with the gateway's "
        "interpreter, e.g. ~/stackchan-gateway/bin/python"
    ) from exc


def auth_headers(token: str | None) -> dict:
    """Bearer header for `token`, or no header at all when there isn't one.

    An empty token is not an error. The gateway disables auth entirely when
    neither STACKCHAN_TOKEN nor BEARER_TOKEN is set, so sending an empty
    `Bearer ` would fail against a server that would have accepted no header.
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


@contextlib.asynccontextmanager
async def open_streams(url: str, headers: dict | None = None):
    """Yield (read, write) for whichever SDK generation is installed."""
    headers = headers or None
    if hasattr(transport_module, "streamable_http_client"):
        # mcp >= 2 (and 1.30): headers travel on a caller-supplied httpx client.
        http_client = transport_module.create_mcp_http_client(headers=headers)
        async with http_client:
            async with transport_module.streamable_http_client(
                url, http_client=http_client
            ) as streams:
                # 1.30 yields three here and 2.x yields two, so index rather
                # than unpack.
                yield streams[0], streams[1]
    elif hasattr(transport_module, "streamablehttp_client"):
        # Older 1.x: headers are a kwarg, and a third stream (the session-id
        # getter) is yielded that no caller here needs.
        async with transport_module.streamablehttp_client(
            url, headers=headers
        ) as streams:
            yield streams[0], streams[1]
    else:
        raise RuntimeError(
            "mcp.client.streamable_http exposes neither streamable_http_client "
            "(2.x) nor streamablehttp_client (1.x) -- unsupported SDK version"
        )


def tool_failed(result) -> bool:
    """True when the tool itself reported failure.

    Checks both spellings on purpose: `is_error` is 2.x, `isError` is 1.x, and
    guessing one turns a failing call into a success on the other.
    """
    for attribute in ("is_error", "isError"):
        value = getattr(result, attribute, None)
        if value is not None:
            return bool(value)
    return False
