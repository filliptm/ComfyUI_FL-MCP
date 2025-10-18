"""PydanticAI Agent setup and management.

This module provides agent creation, configuration, and conversation management
for the FL_JS agentic system.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from contextvars import ContextVar
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from backend.config import settings

logger = logging.getLogger(__name__)

# Context variable for current session_id during tool execution
current_session_id: ContextVar[Optional[str]] = ContextVar('current_session_id', default=None)


class AgentResponse(BaseModel):
    """Agent response model."""
    content: str = Field(..., description="Response content")
    is_final: bool = Field(True, description="Whether this is the final response")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")


class ConversationManager:
    """Manage conversation history for an agent."""
    
    def __init__(self, max_history: int = 50):
        """Initialize conversation manager.
        
        Args:
            max_history: Maximum number of messages to keep
        """
        self.history: List[Dict[str, Any]] = []
        self.max_history = max_history
    
    def add_user_message(self, content: str):
        """Add user message to history.
        
        Args:
            content: Message content
        """
        self.history.append({
            "role": "user",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self._trim_history()
    
    def add_agent_message(self, content: str, tool_calls: Optional[List[Dict]] = None):
        """Add agent message to history.
        
        Args:
            content: Message content
            tool_calls: Optional list of tool calls made
        """
        self.history.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls or [],
            "timestamp": datetime.now().isoformat()
        })
        self._trim_history()
    
    def add_tool_result(self, tool_name: str, result: Any):
        """Add tool execution result to history.
        
        Args:
            tool_name: Name of the tool
            result: Tool result
        """
        self.history.append({
            "role": "tool",
            "tool_name": tool_name,
            "result": result,
            "timestamp": datetime.now().isoformat()
        })
        self._trim_history()
    
    def get_context_messages(self) -> List[Dict[str, Any]]:
        """Get messages formatted for agent context.
        
        Returns:
            List of messages for agent
        """
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
            # Keep recent history
            self.history = self.history[-self.max_history:]
    
    def clear(self):
        """Clear conversation history."""
        self.history = []


def get_llm_model():
    """Get configured LLM model based on settings.
    
    Returns:
        Configured model instance
        
    Raises:
        ValueError: If provider is unknown
    """
    if settings.llm_provider == "openai":
        return OpenAIModel(
            settings.llm_model,
            # api_key=settings.openai_api_key
        )
    elif settings.llm_provider == "openrouter":
        # OpenRouter uses OpenAI-compatible API
        return OpenAIModel(
            settings.llm_model,
            provider=OpenAIProvider(
                base_url='https://openrouter.ai/api/v1',
                api_key=settings.openrouter_api_key
            )
        )
    elif settings.llm_provider == "anthropic":
        # Import here to avoid dependency if not used
        from pydantic_ai.models.anthropic import AnthropicModel
        return AnthropicModel(
            settings.llm_model,
            # api_key=settings.anthropic_api_key
        )
    elif settings.llm_provider == "gemini":
        # Import here to avoid dependency if not used
        from pydantic_ai.models.gemini import GeminiModel
        return GeminiModel(
            settings.llm_model,
            # api_key=settings.google_api_key
        )
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")


def load_system_prompt() -> str:
    """Load system prompt from agents directory.
    
    Returns:
        System prompt with time_now replaced
    """
    time_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    
    # Try to load from agents directory
    prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents", "agent.md")
    
    try:
        with open(prompt_path, "r") as f:
            prompt = f.read()
        prompt = prompt.replace('{time_now}', time_now)
        logger.info(f"Loaded system prompt from {prompt_path}")
        return prompt
    except FileNotFoundError:
        logger.warning(f"System prompt not found at {prompt_path}, using fallback")
        return get_fallback_system_prompt(time_now)


def get_fallback_system_prompt(time_now: str) -> str:
    """Get fallback system prompt if file not found.
    
    Args:
        time_now: Current time string
        
    Returns:
        Fallback system prompt
    """
    return f"""You are an expert ComfyUI workflow assistant.

Current Time: {time_now}

You help users create, modify, and understand ComfyUI workflows through natural language.
You have access to comprehensive tools for node management, manipulation, layout, workflow control, and querying.

Always:
- Query for nodes before modifying them
- Verify operations succeeded
- Be helpful and educational
- Suggest best practices
- Generate diagrams when helpful

## User Skill Level Assessment
- Assess the user's skill level silently
- Based on how they speak about comfyui, you attempt to reply to them at that skill level
- If they don't seem to be interested in learning, don't explain things to them
- You can ask leading questions for this assessment, just never reveal that you are assessing them

## CORE BEHAVIORAL FRAMEWORK: Mode Switching

You must detect and adapt to these example working modes of the user

### Idea Mode
- Personality: creative, everything is possible with the right node!
- Behavior: Exploration and Discovery, make educated suggestions
- Situations that might trigger this mode:
    - User wants to explore a possible new type of workflow
    - After Troubleshooting we realize the problem is not 

### Planning Mode
- Personality: decisive, engineering, meticulous
- Behavior: Interact with the User, Plan workflows
- Think About:
    - The right input nodes (Images? Video?)
    - Positive and Negative Prompts

### Troubleshooting Mode
- Insert preview nodes

### Execution Mode
- Personality: 
- Behavior: Queuing up the workflow, generating and testing output, trying different parameters, asking the user what they think of the output
- Think About:
    - What are the control parameters for these generations: eg. do we set the ksampler seed to random or keep the seed the same and test different cfg settings
    - What 

## Remember While Editing Workflows
- After making layout changes get the workflow overview before attempting to make further layout changes
"""


def create_agent(session_id: str) -> Agent:
    """Create a PydanticAI agent for a session.
    
    Args:
        session_id: Unique session identifier
    
    Returns:
        Configured Agent instance
    """
    logger.info(f"Creating agent for session: {session_id}")
    
    # Get model
    model = get_llm_model()
    
    # Load system prompt
    system_prompt = load_system_prompt()
    
    # Prepare environment for MCP subprocess
    mcp_env = {
        'FL_SESSION_ID': session_id,
        'FL_WS_URL': f'ws://{settings.ws_host}:{settings.ws_port}/ws',
        'FL_MCP_MODE': 'subprocess',  # Flag to indicate subprocess mode
    }
    
    logger.info(f"MCP subprocess environment: session_id={session_id}, ws_url={mcp_env['FL_WS_URL']}")

    # Get absolute path to mcp_server.py
    mcp_server_path = str(Path(__file__).parent / 'mcp_server.py')

    # Launch MCP server with environment
    mcp_servers = [
        MCPServerStdio(
            'python',
            [mcp_server_path],
            env=mcp_env  # Pass environment to subprocess
        )
    ]
    
    # Create agent (tools will be provided via MCP)
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        retries=settings.max_tool_retries,
        mcp_servers=mcp_servers,
    )
    
    # Attach conversation manager
    agent.conversation_manager = ConversationManager(
        max_history=settings.conversation_max_history
    )
    
    # Store session_id
    agent.session_id = session_id
    
    logger.info(f"Agent created for session {session_id}")
    return agent


class AgentManager:
    """Manage agent instances per session."""
    
    def __init__(self):
        """Initialize agent manager."""
        self.agents: Dict[str, Agent] = {}
        logger.info("AgentManager initialized")
    
    def get_agent(self, session_id: str) -> Agent:
        """Get or create agent for session.
        
        Args:
            session_id: Session ID
            
        Returns:
            Agent instance
        """
        if session_id not in self.agents:
            self.agents[session_id] = create_agent(session_id)
            logger.info(f"Created new agent for session: {session_id}")
        return self.agents[session_id]
    
    def remove_agent(self, session_id: str):
        """Remove agent for session.
        
        Args:
            session_id: Session ID
        """
        if session_id in self.agents:
            del self.agents[session_id]
            logger.info(f"Removed agent for session: {session_id}")
    
    def get_agent_count(self) -> int:
        """Get number of active agents.
        
        Returns:
            Number of agents
        """
        return len(self.agents)
    
    def clear_all(self):
        """Clear all agents."""
        count = len(self.agents)
        self.agents.clear()
        logger.info(f"Cleared {count} agents")


# Global agent manager instance
agent_manager = AgentManager()
