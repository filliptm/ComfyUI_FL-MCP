# MCP & Agent Persistence Implementation Guide

## Overview

This document provides the exact code modifications needed to move the MCP subprocess lifecycle from per-message to per-WebSocket-session scope.

**Goal:** Keep the MCP subprocess alive for the entire WebSocket connection instead of spawning it per message.

**Files to modify:** Only `backend/server.py`

**Lines of code changed:** ~25 lines

---

## File: `backend/server.py`

### Modification 1: Move Agent & MCP Context to WebSocket Scope

**Location:** `websocket_endpoint()` function (lines 130-300)

**Current code (lines 201-243):**
```python
        # Register connection with type
        is_reconnect = manager.has_connection(session_id, connection_type)
        context = await manager.connect(websocket, session_id, connection_type)
        
        # Send handshake acknowledgment
        await manager.send_handshake_ack(session_id, is_reconnect, connection_type)
        
        logger.info(
            f"Session {session_id} - {connection_type} "
            f"{'reconnected' if is_reconnect else 'connected'}"
        )
        
        # Message loop
        while True:
            # 🔍 TRACE: Log before receive
            logger.info(f"[TRACE] 📥 Waiting for message on session {session_id} ({connection_type})")
            
            data = await websocket.receive_json()
            
            # 🔍 TRACE: Log what we received
            logger.info(f"[TRACE] 📦 Received message on session {session_id} ({connection_type}): type={data.get('type')}")
            
            # Get message type and session_id for logging
            msg_type = data.get("type")
            msg_session_id = data.get("session_id")
            
            # Log all incoming messages with session_id info (changed from DEBUG to INFO)
            logger.info(
                f"[VALIDATION] Received {msg_type} | "
                f"msg_session_id={msg_session_id} | "
                f"connection_session_id={session_id} | "
                f"connection_type={connection_type}"
            )
            
            # Validate session_id in message
            if msg_session_id != session_id:
                logger.warning(
                    f"[VALIDATION] Session mismatch! "
                    f"msg_session_id={msg_session_id} != connection_session_id={session_id} | "
                    f"msg_type={msg_type}"
                )
                await manager.send_error(
                    session_id,
                    "SESSION_MISMATCH",
                    f"Message session_id '{msg_session_id}' does not match connection session_id '{session_id}'",
                    target=connection_type
                )
                continue
            
            # Route message based on type
            if msg_type == "user_message":
                # await handle_user_message(session_id, data)
                asyncio.create_task(handle_user_message(session_id, data, message_history=context.conversation_history))
```

**New code:**
```python
        # Register connection with type
        is_reconnect = manager.has_connection(session_id, connection_type)
        context = await manager.connect(websocket, session_id, connection_type)
        
        # Send handshake acknowledgment
        await manager.send_handshake_ack(session_id, is_reconnect, connection_type)
        
        logger.info(
            f"Session {session_id} - {connection_type} "
            f"{'reconnected' if is_reconnect else 'connected'}"
        )
        
        # ✅ NEW: Get or create agent for frontend connections ONCE per WebSocket session
        agent = None
        if connection_type == 'frontend':
            agent = agent_manager.get_agent(session_id)
            logger.info(f"Agent created/retrieved for session {session_id}")
        
        # ✅ NEW: Enter MCP context for the entire WebSocket session (if frontend)
        if agent:
            async with agent.run_mcp_servers():
                logger.info(f"MCP servers started for session {session_id}")
                
                # Message loop with persistent MCP connection
                while True:
                    # 🔍 TRACE: Log before receive
                    logger.info(f"[TRACE] 📥 Waiting for message on session {session_id} ({connection_type})")
                    
                    data = await websocket.receive_json()
                    
                    # 🔍 TRACE: Log what we received
                    logger.info(f"[TRACE] 📦 Received message on session {session_id} ({connection_type}): type={data.get('type')}")
                    
                    # Get message type and session_id for logging
                    msg_type = data.get("type")
                    msg_session_id = data.get("session_id")
                    
                    # Log all incoming messages with session_id info (changed from DEBUG to INFO)
                    logger.info(
                        f"[VALIDATION] Received {msg_type} | "
                        f"msg_session_id={msg_session_id} | "
                        f"connection_session_id={session_id} | "
                        f"connection_type={connection_type}"
                    )
                    
                    # Validate session_id in message
                    if msg_session_id != session_id:
                        logger.warning(
                            f"[VALIDATION] Session mismatch! "
                            f"msg_session_id={msg_session_id} != connection_session_id={session_id} | "
                            f"msg_type={msg_type}"
                        )
                        await manager.send_error(
                            session_id,
                            "SESSION_MISMATCH",
                            f"Message session_id '{msg_session_id}' does not match connection session_id '{session_id}'",
                            target=connection_type
                        )
                        continue
                    
                    # Route message based on type
                    if msg_type == "user_message":
                        # ✅ MODIFIED: Pass agent to handler instead of retrieving it
                        asyncio.create_task(
                            handle_user_message(
                                session_id, 
                                data, 
                                message_history=context.conversation_history,
                                agent=agent  # ✅ NEW: Pass the agent
                            )
                        )
```

**Continue after the `if agent:` block for MCP connections:**
```python
        else:
            # MCP connections don't need agent context - use original message loop
            while True:
                # 🔍 TRACE: Log before receive
                logger.info(f"[TRACE] 📥 Waiting for message on session {session_id} ({connection_type})")
                
                data = await websocket.receive_json()
                
                # 🔍 TRACE: Log what we received
                logger.info(f"[TRACE] 📦 Received message on session {session_id} ({connection_type}): type={data.get('type')}")
                
                # Get message type and session_id for logging
                msg_type = data.get("type")
                msg_session_id = data.get("session_id")
                
                # Log all incoming messages with session_id info (changed from DEBUG to INFO)
                logger.info(
                    f"[VALIDATION] Received {msg_type} | "
                    f"msg_session_id={msg_session_id} | "
                    f"connection_session_id={session_id} | "
                    f"connection_type={connection_type}"
                )
                
                # Validate session_id in message
                if msg_session_id != session_id:
                    logger.warning(
                        f"[VALIDATION] Session mismatch! "
                        f"msg_session_id={msg_session_id} != connection_session_id={session_id} | "
                        f"msg_type={msg_type}"
                    )
                    await manager.send_error(
                        session_id,
                        "SESSION_MISMATCH",
                        f"Message session_id '{msg_session_id}' does not match connection session_id '{session_id}'",
                        target=connection_type
                    )
                    continue
                
                # Route message based on type (MCP connections only handle tool_result and tool_request)
                if msg_type == "user_message":
                    # This shouldn't happen for MCP connections
                    logger.warning(f"Received user_message on MCP connection for session {session_id}")
```

**Note:** You'll need to continue the message routing for the rest of the message types in both branches. The key change is:
1. Frontend connections enter the `agent.run_mcp_servers()` context
2. MCP connections skip the agent context (they don't need it)

---

### Modification 2: Update `handle_user_message()` Function

**Location:** `handle_user_message()` function (lines 560-630)

**Current code (lines 560-585):**
```python
async def handle_user_message(session_id: str, data: dict[str, Any], message_history:List[ModelMessage]) -> None:
    """Handle user message.

    Args:
        session_id: Session ID
        data: Message data
    """
    try:
        message = UserMessage(**data)
        logger.info(f"User message from {session_id}: {message.message[:50]}...")
        
        # Set session context for tool callbacks
        current_session_id.set(session_id)
        
        # Send typing indicator
        await manager.send_message(session_id, {
            "type": "typing_indicator",
            "session_id": session_id,
            "is_typing": True,
        })
        
        # Get or create agent for this session
        agent = agent_manager.get_agent(session_id)
        
        response = None
        async with agent.run_mcp_servers():  # ❌ REMOVE THIS LINE
            # Process message with agent
            response = await agent.run(message.message, message_history=filtered_message_history(message_history, include_tool_messages=True))
```

**New code:**
```python
async def handle_user_message(
    session_id: str, 
    data: dict[str, Any], 
    message_history: List[ModelMessage],
    agent: Agent  # ✅ NEW: Receive agent as parameter
) -> None:
    """Handle user message.

    Args:
        session_id: Session ID
        data: Message data
        message_history: Conversation history
        agent: Agent instance (already in MCP context)
    """
    try:
        message = UserMessage(**data)
        logger.info(f"User message from {session_id}: {message.message[:50]}...")
        
        # Set session context for tool callbacks
        current_session_id.set(session_id)
        
        # Send typing indicator
        await manager.send_message(session_id, {
            "type": "typing_indicator",
            "session_id": session_id,
            "is_typing": True,
        })
        
        # ❌ REMOVED: agent = agent_manager.get_agent(session_id)
        # ❌ REMOVED: async with agent.run_mcp_servers():
        
        # ✅ Agent is already in MCP context from WebSocket scope - just call run()
        response = await agent.run(
            message.message, 
            message_history=filtered_message_history(message_history, include_tool_messages=True)
        )
```

**The rest of the function remains unchanged.**

---

### Modification 3: Add Agent Cleanup on Disconnect

**Location:** `websocket_endpoint()` exception handlers (lines 290-310)

**Current code (lines 290-300):**
```python
    except WebSocketDisconnect:
        if session_id:
            manager.disconnect(session_id, connection_type)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            logger.info(f"Session {session_id} - {connection_type} disconnected")
```

**New code:**
```python
    except WebSocketDisconnect:
        if session_id:
            manager.disconnect(session_id, connection_type)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            
            # ✅ NEW: Clean up agent when frontend disconnects
            if connection_type == 'frontend':
                agent_manager.remove_agent(session_id)
                logger.info(f"Agent removed for session {session_id}")
            
            logger.info(f"Session {session_id} - {connection_type} disconnected")
```

**Current code (lines 301-315):**
```python
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e}", exc_info=True)
        if session_id:
            manager.disconnect(session_id, connection_type)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            await manager.send_error(
                session_id,
                "INTERNAL_ERROR",
                "An internal error occurred",
                {"error": str(e)},
                target=connection_type
            )
        try:
            await websocket.close()
        except Exception:
            pass
```

**New code:**
```python
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e}", exc_info=True)
        if session_id:
            manager.disconnect(session_id, connection_type)
            # Cancel any pending callbacks for this session
            if callback_router:
                callback_router.cancel_pending_callbacks(session_id)
            
            # ✅ NEW: Clean up agent on error
            if connection_type == 'frontend':
                agent_manager.remove_agent(session_id)
                logger.info(f"Agent removed for session {session_id} (error cleanup)")
            
            await manager.send_error(
                session_id,
                "INTERNAL_ERROR",
                "An internal error occurred",
                {"error": str(e)},
                target=connection_type
            )
        try:
            await websocket.close()
        except Exception:
            pass
```

---

## Import Changes

**Location:** Top of `backend/server.py` (around line 15)

**Current imports:**
```python
from pydantic_ai import UnexpectedModelBehavior
```

**New imports:**
```python
from pydantic_ai import Agent, UnexpectedModelBehavior
```

**Reason:** We need to type-hint the `agent` parameter in `handle_user_message()`

---

## Summary of Changes

### Lines Modified

| Location | Change | Lines |
|----------|--------|-------|
| `websocket_endpoint()` - after handshake | Add agent creation for frontend | +3 |
| `websocket_endpoint()` - message loop | Wrap in `agent.run_mcp_servers()` context | +3 |
| `websocket_endpoint()` - message loop | Add else branch for MCP connections | +40 |
| `websocket_endpoint()` - user_message routing | Pass `agent` parameter | +1 |
| `websocket_endpoint()` - WebSocketDisconnect | Add agent cleanup | +3 |
| `websocket_endpoint()` - Exception handler | Add agent cleanup | +3 |
| `handle_user_message()` - signature | Add `agent: Agent` parameter | +1 |
| `handle_user_message()` - body | Remove agent retrieval | -1 |
| `handle_user_message()` - body | Remove MCP context manager | -2 |
| Imports | Add `Agent` import | +1 |

**Total:** ~52 lines changed (mostly duplicating the message loop for MCP vs frontend)

---

## Testing Checklist

### Manual Testing

1. **Single message test**
   - [ ] Connect WebSocket from frontend
   - [ ] Send one user message
   - [ ] Verify agent responds
   - [ ] Check logs for "MCP servers started"
   - [ ] Verify only ONE subprocess spawn in logs

2. **Multiple message test**
   - [ ] Connect WebSocket
   - [ ] Send 5 messages in quick succession
   - [ ] Verify all responses received
   - [ ] Check logs - should see ONE subprocess spawn, not five
   - [ ] Measure response time improvement

3. **Disconnect/reconnect test**
   - [ ] Connect WebSocket
   - [ ] Send message
   - [ ] Disconnect
   - [ ] Check logs for "Agent removed for session"
   - [ ] Reconnect with same session_id
   - [ ] Verify new agent/MCP subprocess created

4. **MCP connection test**
   - [ ] Verify MCP subprocess can still connect
   - [ ] Verify it doesn't try to create an agent
   - [ ] Verify tool requests still route properly

5. **Error handling test**
   - [ ] Trigger an error during message processing
   - [ ] Verify agent cleanup happens
   - [ ] Verify no orphaned MCP subprocesses

### Performance Testing

1. **Measure subprocess spawn overhead**
   - Before: Time 10 consecutive messages
   - After: Time 10 consecutive messages
   - Expected: ~90% reduction in total time

2. **Memory leak test**
   - Connect/disconnect 100 times
   - Monitor process count (`ps aux | grep python`)
   - Verify no orphaned MCP subprocesses

3. **Concurrent sessions test**
   - Connect 10 WebSocket sessions simultaneously
   - Send messages from each
   - Verify 10 MCP subprocesses (one per session)
   - Verify no cross-session interference

---

## Rollback Plan

If issues occur, revert these changes:

1. Remove `agent` parameter from `handle_user_message()`
2. Re-add `agent = agent_manager.get_agent(session_id)` in handler
3. Re-add `async with agent.run_mcp_servers():` in handler
4. Remove agent creation/context from `websocket_endpoint()`
5. Remove agent cleanup from disconnect handlers

Git command: `git revert <commit_hash>`

---

## Expected Behavior After Implementation

### Log Output (First Message)
```
INFO - Session abc123 - frontend connected
INFO - Agent created/retrieved for session abc123
INFO - MCP servers started for session abc123
INFO - User message from abc123: Hello...
INFO - Agent response sent to abc123
```

### Log Output (Subsequent Messages)
```
INFO - User message from abc123: Another question...
INFO - Agent response sent to abc123
```

**Notice:** No "MCP servers started" log on subsequent messages!

### Log Output (Disconnect)
```
INFO - Session abc123 - frontend disconnected
INFO - Agent removed for session abc123
```

---

## Performance Expectations

### Before (Per-Message Spawn)
- Message 1: 500ms (spawn + init + execution)
- Message 2: 500ms (spawn + init + execution)
- Message 3: 500ms (spawn + init + execution)
- **Total for 3 messages:** ~1500ms

### After (Persistent MCP)
- Message 1: 500ms (spawn + init + execution)
- Message 2: 50ms (execution only)
- Message 3: 50ms (execution only)
- **Total for 3 messages:** ~600ms

**Improvement:** 60% faster for 3 messages, scales better with more messages

---

## Notes

1. **Message loop duplication**: We duplicate the message routing logic for frontend vs MCP connections. This could be refactored later, but for now it's clearer to keep them separate.

2. **Agent lifecycle**: The agent now lives for the entire WebSocket session. If you need to reset the agent mid-session, you'll need to add a mechanism for that.

3. **Memory management**: With persistent agents, memory usage per session will be slightly higher (agent + MCP subprocess stays in memory). Monitor this in production.

4. **Async task handling**: We still use `asyncio.create_task()` for message handling. This means messages are processed asynchronously, which is fine as long as the WebSocket stays connected.

5. **Error recovery**: If the MCP subprocess crashes, the context manager will exit and the WebSocket will disconnect. The frontend will need to reconnect to get a new agent/MCP subprocess.

---

## Future Optimizations

1. **Agent pooling**: Implement LRU cache to limit total number of persistent agents
2. **Health checks**: Ping MCP subprocess periodically to detect crashes
3. **Graceful degradation**: If MCP subprocess dies, try to restart it without disconnecting WebSocket
4. **Metrics**: Add Prometheus metrics for agent lifecycle events
5. **Configuration**: Make agent persistence configurable (some users might prefer per-message spawn)
