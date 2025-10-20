Short answer: yes—don’t create a new `McpStdio` client per message. Start (and keep) a single MCP session alive and reuse it across websocket messages. In FastAPI this is easiest with an app-level lifespan (or startup) hook that:

1. spawns the MCP server once (stdio),
2. opens one `ClientSession` (or whatever your pydantic-ai/FastMCP wrapper returns),
3. builds your Agent with that connected tool client,
4. stores the agent/session in `app.state`,
5. reuses it in every websocket handler message.

Below is a minimal, production-ish pattern you can adapt (names may differ slightly depending on the MCP SDK you’re using, but the structure is what matters).

---

### App-level, long-lived MCP session

```python
# app.py
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

# --- MCP stdio client bits (SDK names vary; adapt to your SDK) ---
# Example shape for python MCP:
# from mcp.client.session import ClientSession
# from mcp.client.stdio import stdio_client
#
# async with stdio_client(command="python", args=["-m", "your_mcp_server"]) as (read, write):
#     async with ClientSession(read, write) as session:
#         await session.initialize()

# --- Your agent (pydantic-ai) factory ---
# from pydantic_ai import Agent
# def build_agent(mcp_session) -> Agent: ...

class MCPBundle:
    """Holds the long-lived MCP transport + session + agent."""
    def __init__(self):
        self._lock = asyncio.Lock()
        self.session = None
        self.agent = None
        self.close_cb = None  # optional: to stop the spawned server

    async def start(self):
        async with self._lock:
            if self.session:
                return  # already started

            # 1) Spawn your MCP server via stdio exactly once
            #     Replace with your SDK’s way to spawn/connect:
            #
            # self.close_cb, self.session = await spawn_and_connect_stdio(
            #     command="python", args=["-m", "your_mcp_server"]
            # )
            #
            # async def spawn_and_connect_stdio(...): return (close_cb, session)

            # PSEUDO (replace with your SDK):
            self.close_cb = lambda: None
            self.session = object()  # <- your real MCP ClientSession here

            # 2) Initialize the session (capabilities, tools, resources)
            # await self.session.initialize()

            # 3) Build a single agent bound to this live MCP session
            # self.agent = build_agent(self.session)

    async def ensure_alive(self):
        """Optionally ping; if dead, restart."""
        # If your SDK exposes a "ping"/"list_tools" you can use it here.
        # try:
        #     await self.session.list_tools()
        # except Exception:
        #     await self.restart()
        pass

    async def restart(self):
        async with self._lock:
            await self.stop()
            await self.start()

    async def stop(self):
        async with self._lock:
            if self.session:
                # await self.session.close()
                self.session = None
            if self.close_cb:
                try:
                    self.close_cb()
                finally:
                    self.close_cb = None
            self.agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mcp = MCPBundle()
    await app.state.mcp.start()
    try:
        yield
    finally:
        await app.state.mcp.stop()


app = FastAPI(lifespan=lifespan)

# Simple connection registry to route multiple websockets to the same agent
clients = set()

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        # Optionally check session liveness (quick ping) and restart if needed
        await app.state.mcp.ensure_alive()

        # You now reuse the ONE agent for all messages
        while True:
            if ws.application_state == WebSocketState.DISCONNECTED:
                break
            msg = await ws.receive_text()

            # Run your pydantic-ai agent with the persistent MCP tools:
            # result = await app.state.mcp.agent.run(msg)
            result = {"text": f"(pretend agent answer to) {msg}"}

            await ws.send_json(result)

    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
```

**Key idea:** construct the MCP session + agent once in `lifespan/startup` and keep it alive. Each websocket message just calls `agent.run(...)`. No per-message process spawn.

---

### When you *need* more than one session

If tools are stateful per user/workspace, keep **one live session per key** (e.g., `user_id` or `project_id`) in a dictionary with LRU eviction:

```python
from collections import OrderedDict
class MCPPool(OrderedDict):
    MAX = 12  # tune for memory/CPU

    async def get_or_create(self, key):
        if key in self:
            bundle = self.pop(key)
            self[key] = bundle
            return bundle
        bundle = MCPBundle()
        await bundle.start()
        self[key] = bundle
        if len(self) > self.MAX:
            old_key, old_bundle = self.popitem(last=False)
            await old_bundle.stop()
        return bundle
```

Then route each websocket to the right `MCPBundle` by user/workspace.

---

### Why this works (and what to watch)

* **`McpStdio` spawns a process per connection.** If you connect once at app startup, you keep that process alive and avoid N× spawns.
* **Concurrency:** run an `asyncio.Lock` inside the bundle to prevent overlapping reconnects. The agent’s `run` calls should already be `await`ed; if the MCP server is not thread-safe, serialize calls or shard across a small pool.
* **Resilience:** add a light heartbeat (e.g., `list_tools`) every N seconds or just on demand via `ensure_alive()`. If the child process dies, call `restart()`.
* **Hot reload / deploys:** when the server code changes, you’ll need to restart the FastAPI app (or explicitly call `restart()`).
* **Throughput scaling:** if a single stdio process becomes a bottleneck, run the MCP server as a **daemon** (its own long-running service) and connect via a socket/WebSocket transport rather than stdio; or keep a **small pool** of stdio sessions and round-robin requests.

---

### TL;DR implementation checklist

* [ ] Move your MCP connect logic from “per request/per message” to FastAPI `lifespan` (or `on_event("startup")`).
* [ ] Keep the `ClientSession` and agent object in `app.state`.
* [ ] Route all websocket messages to that one agent.
* [ ] Add a minimal “ensure alive” + “restart on failure”.
* [ ] If you need user isolation, use a keyed pool of long-lived sessions.

If you show me your current `McpStdio` connect code (just the part where you spawn and bind it to the agent), I’ll drop it straight into this pattern for you.
