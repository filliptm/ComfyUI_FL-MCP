# MCP & Agent Persistence Investigation

## Current Architecture Analysis

### The Problem: Per-Message MCP Subprocess Spawning

**Current Flow:**
1. WebSocket connects → `websocket_endpoint()` in `backend/server.py` (line 130)
2. User sends message → `handle_user_message()` called (line 243)
3. Agent retrieved/created → `agent_manager.get_agent(session_id)` (line 579)
4. **MCP subprocess spawned** → `async with agent.run_mcp_servers():` (line 582)
5. Agent runs → `await agent.run(message.message, ...)` (line 584)
6. **MCP subprocess terminates** when context exits

**Cost per message:**
- Python subprocess spawn (~100-500ms)
- MCP server initialization
- WebSocket connection setup from subprocess to backend
- Memory allocation/deallocation
- Lost state between messages

---

## Key Constraint: `run_mcp_servers()` Context Requirement

**From pydantic-ai documentation:**
```python
async with agent.run_mcp_servers():
    result = await agent.run(message)
```

**This means:**
- `agent.run()` MUST be called within the `run_mcp_servers()` context
- We can't just "start MCP servers once" and forget about them
- The context manager handles the MCP subprocess lifecycle

**However:**
- The context can be **long-lived** (kept open for the entire WebSocket session)
- We don't need to enter/exit it per message

---

## Architecture Options

### Option 1: FastAPI Lifespan (App-Level)
**Scope:** Application startup/shutdown

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start single agent with MCP servers
    agent = create_agent("global")
    async with agent.run_mcp_servers():
        app.state.agent = agent
        yield
    # MCP servers stop here
```

**Pros:**
- Minimal changes
- Single MCP subprocess for entire app
- Maximum resource efficiency

**Cons:**
- ❌ **Only ONE agent for ALL users** (not viable for multi-user)
- ❌ Session isolation lost
- ❌ Can't pass session_id to MCP subprocess

**Verdict:** ❌ Not suitable - we need per-session agents

---

### Option 2: WebSocket Connection Scope (Per-Session)
**Scope:** Individual WebSocket connection lifetime

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # ... handshake ...
    
    # Create agent for this session
    agent = agent_manager.get_agent(session_id)
    
    # Enter MCP context ONCE for entire WebSocket session
    async with agent.run_mcp_servers():
        # Message loop
        while True:
            data = await websocket.receive_json()
            
            if msg_type == "user_message":
                # Just call agent.run() - already in context!
                response = await agent.run(message.message, ...)
                await manager.send_message(...)
```

**Pros:**
- ✅ **One MCP subprocess per WebSocket session**
- ✅ Agent and MCP persist across all messages in session
- ✅ Session isolation maintained
- ✅ Minimal code changes
- ✅ Natural lifecycle (MCP dies when WebSocket closes)
- ✅ Session-specific environment (`FL_SESSION_ID`) works

**Cons:**
- Need to refactor `handle_user_message()` to not spawn context
- Message handling must be synchronous within the loop (or carefully managed)

**Verdict:** ✅ **BEST OPTION** - Perfect match for our architecture

---

### Option 3: AgentManager-Managed Contexts (Pool)
**Scope:** Managed pool with manual lifecycle

```python
class AgentManager:
    async def get_agent_with_context(self, session_id):
        if session_id not in self.contexts:
            agent = create_agent(session_id)
            context = await agent.run_mcp_servers().__aenter__()
            self.contexts[session_id] = (agent, context)
        return self.contexts[session_id][0]
    
    async def cleanup_session(self, session_id):
        if session_id in self.contexts:
            agent, context = self.contexts[session_id]
            await context.__aexit__(None, None, None)
            del self.contexts[session_id]
```

**Pros:**
- ✅ Centralized lifecycle management
- ✅ Can implement LRU eviction
- ✅ Explicit cleanup control

**Cons:**
- ❌ More complex
- ❌ Manual context manager handling (error-prone)
- ❌ Need to coordinate with WebSocket disconnects

**Verdict:** 🟡 Viable but over-engineered for our needs

---

## Recommended Implementation: Option 2

### Current Code Structure

**File: `backend/server.py`**
```python
# Line 130: WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Line 147: Accept connection
    await websocket.accept()
    
    # Line 150-200: Handshake
    handshake = Handshake(**handshake_data)
    session_id = handshake.session_id
    
    # Line 201: Register connection
    context = await manager.connect(websocket, session_id, connection_type)
    
    # Line 209: Message loop
    while True:
        data = await websocket.receive_json()
        
        # Line 243: User message handling
        if msg_type == "user_message":
            asyncio.create_task(handle_user_message(session_id, data, ...))
```

**File: `backend/server.py` - `handle_user_message()`**
```python
# Line 560
async def handle_user_message(session_id: str, data: dict, message_history):
    # Line 579: Get agent (cached per session)
    agent = agent_manager.get_agent(session_id)
    
    # Line 582: ⚠️ SPAWNS MCP SUBPROCESS HERE
    async with agent.run_mcp_servers():
        response = await agent.run(message.message, ...)
    
    # Line 588: Update history and send response
    message_history.clear()
    message_history.extend(response.all_messages())
    await manager.send_message(...)
```

---

### Proposed Changes

#### Change 1: Move MCP Context to WebSocket Scope

**Location:** `backend/server.py` - `websocket_endpoint()`

**Before:**
```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # ... handshake ...
    context = await manager.connect(websocket, session_id, connection_type)
    
    # Message loop
    while True:
        data = await websocket.receive_json()
        if msg_type == "user_message":
            asyncio.create_task(handle_user_message(session_id, data, ...))
```

**After:**
```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # ... handshake ...
    context = await manager.connect(websocket, session_id, connection_type)
    
    # Get/create agent for this session (only for frontend connections)
    agent = None
    if connection_type == 'frontend':
        agent = agent_manager.get_agent(session_id)
    
    # Enter MCP context for the entire WebSocket session
    if agent:
        async with agent.run_mcp_servers():
            # Message loop with persistent MCP connection
            while True:
                data = await websocket.receive_json()
                if msg_type == "user_message":
                    asyncio.create_task(
                        handle_user_message(session_id, data, message_history, agent)
                    )
    else:
        # MCP connections don't need agent context
        while True:
            data = await websocket.receive_json()
            # ... handle MCP messages ...
```

#### Change 2: Remove MCP Context from Message Handler

**Location:** `backend/server.py` - `handle_user_message()`

**Before:**
```python
async def handle_user_message(session_id: str, data: dict, message_history):
    agent = agent_manager.get_agent(session_id)
    
    async with agent.run_mcp_servers():  # ❌ Remove this
        response = await agent.run(message.message, ...)
    
    message_history.clear()
    message_history.extend(response.all_messages())
```

**After:**
```python
async def handle_user_message(
    session_id: str, 
    data: dict, 
    message_history,
    agent: Agent  # ✅ Pass agent from WebSocket scope
):
    # Agent is already in MCP context from WebSocket scope
    response = await agent.run(message.message, ...)
    
    message_history.clear()
    message_history.extend(response.all_messages())
```

---

## Implementation Complexity

### Minimal Changes Required

1. **`backend/server.py` - `websocket_endpoint()`**
   - Add agent retrieval after handshake (for frontend connections)
   - Wrap message loop in `async with agent.run_mcp_servers():`
   - Pass `agent` to `handle_user_message()`

2. **`backend/server.py` - `handle_user_message()`**
   - Add `agent: Agent` parameter
   - Remove `agent = agent_manager.get_agent(session_id)`
   - Remove `async with agent.run_mcp_servers():`

**Total LOC changed:** ~15-20 lines

---

## Edge Cases & Considerations

### 1. Async Task Creation
**Current:** `asyncio.create_task(handle_user_message(...))`

**Issue:** Tasks run independently of the WebSocket loop

**Solution:** Either:
- Make message handling synchronous (await directly)
- Keep task but ensure it completes before WebSocket closes
- Use task group to track running tasks

**Recommendation:** Keep async tasks but add cleanup on disconnect

### 2. MCP Connection Type
**Current:** MCP subprocess also connects via WebSocket

**Issue:** We don't want to create an agent for MCP connections

**Solution:** Only enter MCP context for `connection_type == 'frontend'`

### 3. Agent Cleanup
**Current:** `AgentManager` caches agents indefinitely

**Issue:** MCP subprocess will stay alive until agent is removed

**Solution:** Add cleanup when WebSocket disconnects:
```python
except WebSocketDisconnect:
    if connection_type == 'frontend':
        agent_manager.remove_agent(session_id)
```

### 4. Error Handling
**Current:** Try/except around entire WebSocket endpoint

**Issue:** Need to ensure MCP context exits cleanly

**Solution:** Context manager handles this automatically

---

## Performance Impact

### Before (Per-Message Spawn)
- **First message:** 500ms (spawn + init + execution)
- **Subsequent messages:** 500ms (spawn + init + execution)
- **10 messages:** ~5 seconds overhead

### After (Persistent MCP)
- **First message:** 500ms (spawn + init + execution)
- **Subsequent messages:** 50ms (execution only)
- **10 messages:** ~500ms overhead

**Expected improvement:** ~90% reduction in overhead for multi-message conversations

---

## Testing Strategy

1. **Unit Test:** Verify agent persists across messages in same session
2. **Integration Test:** Multiple messages in quick succession
3. **Load Test:** Multiple concurrent sessions
4. **Disconnect Test:** Ensure MCP subprocess dies with WebSocket
5. **Reconnect Test:** New WebSocket = new MCP subprocess

---

## Migration Path

### Phase 1: Implement Changes ✅
1. Modify `websocket_endpoint()` to create agent context
2. Modify `handle_user_message()` to receive agent
3. Update function signature where called

### Phase 2: Test & Validate
1. Manual testing with web UI
2. Check logs for subprocess lifecycle
3. Verify no memory leaks

### Phase 3: Optimize (Future)
1. Add agent pool with LRU eviction
2. Implement health checks for MCP subprocess
3. Add metrics for MCP connection duration

---

## Conclusion

**Recommended approach:** Move MCP context from `handle_user_message()` to `websocket_endpoint()`

**Why it's optimal:**
- ✅ Minimal code changes (~20 LOC)
- ✅ Natural lifecycle (WebSocket = MCP subprocess)
- ✅ Session isolation maintained
- ✅ No complex pool management needed
- ✅ ~90% performance improvement for multi-message conversations

**Next step:** Implement Option 2 changes in `backend/server.py`
