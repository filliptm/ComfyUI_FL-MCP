# PydanticAI Agent Setup Implementation Plan

## Agent Architecture

### Core Components
1. **Agent Factory** - Creates agent instances per session
2. **System Prompt** - Defines agent behavior and capabilities
3. **Tool Registry** - All MCP tools available to agent
4. **Context Manager** - Maintains conversation history
5. **Response Processor** - Formats agent responses

## Agent Configuration

### Agent Factory

```python
# backend/agent.py

from pydantic_ai import Agent
from pydantic_ai.models import OpenAIModel, AnthropicModel, GeminiModel
from backend.config import settings
from backend.mcp_server import mcp
from typing import Dict, Any, List
import json

def get_llm_model():
    """Get configured LLM model based on settings."""
    if settings.llm_provider == "openai":
        return OpenAIModel(
            model_name=settings.llm_model,
            api_key=settings.openai_api_key
        )
    elif settings.llm_provider == "anthropic":
        return AnthropicModel(
            model_name=settings.llm_model,
            api_key=settings.anthropic_api_key
        )
    elif settings.llm_provider == "gemini":
        return GeminiModel(
            model_name=settings.llm_model,
            api_key=settings.google_api_key
        )
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")

def create_agent(session_id: str) -> Agent:
    """
    Create a PydanticAI agent for a session.
    
    Args:
        session_id: Unique session identifier
    
    Returns:
        Configured Agent instance
    """
    agent = Agent(
        model=get_llm_model(),
        system_prompt=get_system_prompt(),
        tools=mcp.get_tools(),
        result_type=AgentResponse,
        retries=settings.max_tool_retries
    )
    
    # Store session_id in agent context
    agent.context = {
        "session_id": session_id,
        "conversation_history": [],
        "workflow_state": {},
        "last_tool_results": []
    }
    
    return agent

class AgentResponse(BaseModel):
    """Agent response model."""
    content: str = Field(..., description="Response content")
    is_final: bool = Field(True, description="Whether this is the final response")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
```

## System Prompt

### Comprehensive System Prompt

```python
# backend/agent.py

def get_system_prompt() -> str:
    return """
You are an expert ComfyUI workflow assistant. You help users create, modify, and understand 
ComfyUI workflows through natural language conversation.

## Your Capabilities

You have access to a comprehensive set of tools to manipulate ComfyUI workflows:

### Node Management
- Create, find, remove, bypass, pin, and select nodes
- All ComfyUI node types are available

### Node Manipulation  
- Get and set node parameter values
- Connect nodes together
- Modify node properties

### Layout Management
- Position nodes relative to each other
- Arrange workflow layouts
- Manage node sizes and positions

### Workflow Control
- Queue workflows for execution
- Cancel running workflows
- Configure batch processing
- Monitor execution status

### Workflow Query & Analysis
- Query nodes using structured filters
- Traverse node connections
- Generate workflow diagrams
- Get workflow statistics

## Query Language

When querying workflows, use JSON-based query objects:

### Find all nodes of a type:
```json
{
  "filters": {
    "operator": "and",
    "filters": [{"field": "type", "operator": "equals", "value": "KSampler"}]
  }
}
```

### Find nodes with specific parameters:
```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {"field": "type", "operator": "equals", "value": "CheckpointLoaderSimple"},
      {"field": "parameters.ckpt_name", "operator": "contains", "value": "sd15"}
    ]
  }
}
```

### Traverse connections:
```json
{
  "filters": {
    "operator": "and",
    "filters": [{"field": "id", "operator": "equals", "value": 5}]
  },
  "traversal": {
    "direction": "downstream",
    "max_depth": null
  }
}
```

## Interaction Guidelines

### Be Proactive
- Suggest improvements to workflows
- Warn about potential issues (disconnected nodes, missing connections)
- Offer to fix problems you detect

### Be Precise
- Always specify exact node IDs when manipulating nodes
- Use query tools to find nodes before modifying them
- Verify node existence before operations

### Be Efficient
- Batch operations when possible
- Use traversal for finding connected nodes
- Generate diagrams to help users visualize workflows

### Handle Errors Gracefully
- If a tool fails, explain why and suggest alternatives
- If a node doesn't exist, help the user find or create it
- If connections are invalid, explain the type mismatch

### Workflow Best Practices
- Checkpoint loaders at the start
- Samplers in the middle
- VAE decode and save at the end
- Proper connection types (MODEL->model, CLIP->clip, etc.)
- Reasonable parameter values (steps: 20-50, cfg: 6-12, etc.)

## Common Workflows

### Text-to-Image (Basic)
1. CheckpointLoaderSimple
2. CLIPTextEncode (positive)
3. CLIPTextEncode (negative)  
4. EmptyLatentImage
5. KSampler
6. VAEDecode
7. SaveImage

### Text-to-Image (Advanced)
Add: LoraLoader, ControlNet, Upscalers, etc.

### Image-to-Image
Replace EmptyLatentImage with LoadImage + VAEEncode

## Response Format

### For Questions
- Answer directly and concisely
- Use diagrams when helpful
- Provide specific node IDs and values

### For Commands
- Execute the requested actions
- Confirm what was done
- Report any issues encountered

### For Complex Tasks
- Break down into steps
- Execute step by step
- Provide progress updates
- Summarize final result

## Tool Usage Strategy

### Before Creating Nodes
1. Query to check if similar nodes exist
2. Plan the workflow structure
3. Create nodes in logical order (left to right)
4. Connect as you go or all at once

### Before Modifying Nodes
1. Use query_workflow to find target nodes
2. Verify node IDs are correct
3. Get current values if needed
4. Make changes
5. Verify changes succeeded

### Before Executing
1. Validate workflow (check for disconnected nodes)
2. Verify all required inputs are connected
3. Check parameter values are reasonable
4. Queue workflow
5. Monitor execution

### When Errors Occur
1. Analyze the error message
2. Identify the root cause
3. Suggest specific fixes
4. Offer to implement fixes automatically
5. Verify fix resolved the issue

## Examples

### User: "Create a simple txt2img workflow"
You should:
1. Create CheckpointLoaderSimple
2. Create two CLIPTextEncode nodes
3. Create EmptyLatentImage
4. Create KSampler
5. Create VAEDecode
6. Create SaveImage
7. Connect all nodes properly
8. Set reasonable default parameters
9. Confirm completion with diagram

### User: "Change all KSampler steps to 30"
You should:
1. Query for all KSampler nodes
2. For each found node, set steps to 30
3. Confirm how many nodes were updated

### User: "Show me the workflow"
You should:
1. Use workflow_overview tool
2. Return the Mermaid diagram
3. Optionally provide summary statistics

### User: "Why isn't this working?"
You should:
1. Query workflow for validation issues
2. Check for disconnected nodes
3. Verify all required inputs are connected
4. Check parameter values
5. Identify specific issues
6. Suggest fixes

## Remember
- You're working with a live workflow in the user's browser
- Changes are immediate and visible
- Always confirm actions taken
- Be helpful and educational
- Suggest best practices
- Make workflows that actually work!
"""
```

## Context Management

### Conversation History

```python
# backend/agent.py

from typing import List, Dict, Any
from datetime import datetime

class ConversationManager:
    """Manage conversation history for an agent."""
    
    def __init__(self, max_history: int = 50):
        self.history: List[Dict[str, Any]] = []
        self.max_history = max_history
    
    def add_user_message(self, content: str):
        """Add user message to history."""
        self.history.append({
            "role": "user",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self._trim_history()
    
    def add_agent_message(self, content: str, tool_calls: List[Dict] = None):
        """Add agent message to history."""
        self.history.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls or [],
            "timestamp": datetime.now().isoformat()
        })
        self._trim_history()
    
    def add_tool_result(self, tool_name: str, result: Any):
        """Add tool execution result to history."""
        self.history.append({
            "role": "tool",
            "tool_name": tool_name,
            "result": result,
            "timestamp": datetime.now().isoformat()
        })
        self._trim_history()
    
    def get_context_messages(self) -> List[Dict[str, Any]]:
        """Get messages formatted for agent context."""
        return [
            {
                "role": msg["role"],
                "content": msg.get("content", "")
            }
            for msg in self.history
            if msg["role"] in ["user", "assistant"]
        ]
    
    def _trim_history(self):
        """Keep history within max length."""
        if len(self.history) > self.max_history:
            # Keep system message + recent history
            self.history = self.history[-self.max_history:]
    
    def clear(self):
        """Clear conversation history."""
        self.history = []
```

## Message Processing

### User Message Handler

```python
# backend/websocket.py

async def handle_user_message(
    websocket: WebSocket,
    session_id: str,
    agent: Agent,
    data: dict
):
    """
    Process user message with agent.
    
    Args:
        websocket: WebSocket connection
        session_id: Session ID
        agent: Agent instance
        data: Message data
    """
    from contextvars import ContextVar
    
    # Set session_id in context for tool callbacks
    session_id_var: ContextVar[str] = ContextVar('session_id')
    session_id_var.set(session_id)
    
    user_message = data.get("content")
    
    # Add to conversation history
    if not hasattr(agent, 'conversation_manager'):
        agent.conversation_manager = ConversationManager()
    
    agent.conversation_manager.add_user_message(user_message)
    
    # Send typing indicator
    await manager.send_message(session_id, {
        "type": "typing_indicator",
        "session_id": session_id,
        "is_typing": True
    })
    
    try:
        # Run agent with conversation context
        result = await agent.run(
            user_message,
            message_history=agent.conversation_manager.get_context_messages()
        )
        
        # Add agent response to history
        agent.conversation_manager.add_agent_message(
            result.content,
            tool_calls=getattr(result, 'tool_calls', [])
        )
        
        # Send response
        await manager.send_message(session_id, {
            "type": "agent_response",
            "session_id": session_id,
            "content": result.content,
            "is_final": True,
            "metadata": result.metadata
        })
    
    except Exception as e:
        # Send error
        await manager.send_message(session_id, {
            "type": "error",
            "session_id": session_id,
            "error_code": "AGENT_ERROR",
            "message": str(e)
        })
    
    finally:
        # Stop typing indicator
        await manager.send_message(session_id, {
            "type": "typing_indicator",
            "session_id": session_id,
            "is_typing": False
        })
```

## Streaming Responses (Optional)

```python
# backend/agent.py

async def stream_agent_response(
    agent: Agent,
    user_message: str,
    session_id: str,
    websocket_manager
):
    """
    Stream agent response token by token.
    
    Args:
        agent: Agent instance
        user_message: User's message
        session_id: Session ID
        websocket_manager: WebSocket manager for sending updates
    """
    accumulated_content = ""
    
    async for chunk in agent.run_stream(user_message):
        if chunk.content:
            accumulated_content += chunk.content
            
            # Send partial response
            await websocket_manager.send_message(session_id, {
                "type": "agent_response",
                "session_id": session_id,
                "content": chunk.content,
                "is_final": False
            })
    
    # Send final marker
    await websocket_manager.send_message(session_id, {
        "type": "agent_response",
        "session_id": session_id,
        "content": "",
        "is_final": True
    })
    
    return accumulated_content
```

## Agent Utilities

### Response Formatting

```python
# backend/utils.py

def format_mermaid_response(diagram: str) -> str:
    """
    Format Mermaid diagram for chat display.
    
    Args:
        diagram: Mermaid diagram string
    
    Returns:
        Formatted markdown with mermaid code block
    """
    return f"```mermaid\n{diagram}\n```"

def format_tool_result(tool_name: str, result: Any) -> str:
    """
    Format tool result for display.
    
    Args:
        tool_name: Name of the tool
        result: Tool result
    
    Returns:
        Formatted string
    """
    if isinstance(result, dict):
        return f"**{tool_name}** result:\n```json\n{json.dumps(result, indent=2)}\n```"
    elif isinstance(result, list):
        return f"**{tool_name}** returned {len(result)} items"
    else:
        return f"**{tool_name}**: {result}"

def format_error(error: Exception) -> str:
    """
    Format error for user display.
    
    Args:
        error: Exception object
    
    Returns:
        User-friendly error message
    """
    error_messages = {
        "TimeoutError": "The operation took too long and was cancelled.",
        "RuntimeError": "An error occurred while executing the operation.",
        "ValueError": "Invalid parameter value provided.",
        "KeyError": "Required parameter is missing."
    }
    
    error_type = type(error).__name__
    base_message = error_messages.get(error_type, "An unexpected error occurred.")
    
    return f"{base_message}\n\nDetails: {str(error)}"
```

## Agent Configuration

### Settings

```python
# backend/config.py

from pydantic_settings import BaseSettings
from typing import Literal

class Settings(BaseSettings):
    # LLM Provider
    llm_provider: Literal["openai", "anthropic", "gemini"] = "openai"
    llm_model: str = "gpt-4-turbo-preview"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4000
    
    # API Keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    
    # Agent settings
    max_tool_retries: int = 3
    conversation_max_history: int = 50
    
    # WebSocket settings
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000
    ws_heartbeat_interval: int = 30
    ws_session_timeout: int = 300
    
    # Tool execution
    tool_timeout: int = 30000  # milliseconds
    
    class Config:
        env_file = ".env"

settings = Settings()
```

## Testing Agent

### Unit Tests

```python
# tests/backend/test_agent.py

import pytest
from backend.agent import create_agent, get_system_prompt

@pytest.mark.asyncio
async def test_agent_creation():
    """Test agent creation."""
    agent = create_agent("test-session")
    assert agent is not None
    assert agent.context["session_id"] == "test-session"

@pytest.mark.asyncio
async def test_system_prompt():
    """Test system prompt generation."""
    prompt = get_system_prompt()
    assert "ComfyUI" in prompt
    assert "tools" in prompt.lower()

@pytest.mark.asyncio  
async def test_agent_response():
    """Test agent response to simple query."""
    agent = create_agent("test-session")
    result = await agent.run("What can you help me with?")
    assert result.content
    assert len(result.content) > 0
```

## Summary

✅ **Agent factory** creates instances per session
✅ **Comprehensive system prompt** with examples and guidelines
✅ **Conversation history** management
✅ **Context preservation** across messages
✅ **Tool integration** via FastMCP
✅ **Error handling** and formatting
✅ **Streaming support** (optional)
✅ **Configurable** via environment variables
✅ **Testable** with unit tests
