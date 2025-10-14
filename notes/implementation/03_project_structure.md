# Project Structure Implementation Plan

## Directory Structure

```
fl_js/
├── backend/
│   ├── __init__.py
│   ├── server.py              # FastAPI application
│   ├── websocket.py           # WebSocket connection manager
│   ├── agent.py               # PydanticAI agent setup
│   ├── mcp_server.py          # FastMCP tool definitions
│   ├── models.py              # Pydantic models (messages, queries)
│   ├── config.py              # Configuration settings
│   ├── callback_router.py     # Tool callback routing
│   └── utils.py               # Utility functions
│
├── frontend/
│   ├── chat_ui.js             # Chat sidebar UI component
│   ├── ws_client.js           # WebSocket client
│   ├── session_manager.js     # Session management
│   ├── tool_executor.js       # Tool execution handler
│   ├── query_executor.js      # Query DSL executor
│   ├── fl_api.js              # FL_JS API wrapper
│   ├── diagram_generator.js   # Mermaid diagram generation
│   └── styles.css             # UI styles
│
├── legacy/
│   ├── FL_JS.py               # Original FL_JS node
│   ├── FL_WF_Agent.py         # Original FL_WF_Agent node
│   ├── fl_js.js               # Original FL_JS functions
│   └── fl_wf_agent.js         # Original agent script
│
├── tests/
│   ├── backend/
│   │   ├── test_websocket.py
│   │   ├── test_agent.py
│   │   ├── test_mcp_tools.py
│   │   └── test_query_executor.py
│   ├── frontend/
│   │   ├── test_ws_client.js
│   │   ├── test_tool_executor.js
│   │   └── test_query_executor.js
│   └── integration/
│       ├── test_e2e_workflow.py
│       └── test_multi_client.py
│
├── notes/
│   ├── roadmap.md
│   ├── diagrams.md
│   ├── looking_forward.md
│   └── implementation/
│       ├── 01_websocket_protocol.md
│       ├── 02_query_dsl.md
│       ├── 03_project_structure.md
│       ├── 04_mcp_tools.md
│       ├── 05_agent_setup.md
│       └── 06_frontend_ui.md
│
├── .env.example               # Environment variables template
├── .gitignore
├── requirements.txt           # Python dependencies
├── pyproject.toml            # Python project config
├── README.md
└── package.json              # For frontend tooling (optional)
```

## Backend Structure Detail

### server.py
```python
"""
Main FastAPI application entry point.

Responsibilities:
- Initialize FastAPI app
- Configure CORS
- Register WebSocket endpoint
- Health check endpoints
- Background tasks (session cleanup)
"""
```

### websocket.py
```python
"""
WebSocket connection management.

Responsibilities:
- ConnectionManager class
- Session registration/cleanup
- Message routing by session_id
- Heartbeat monitoring
- Reconnection handling
"""
```

### agent.py
```python
"""
PydanticAI agent initialization and management.

Responsibilities:
- Agent factory function
- System prompt configuration
- Tool registration
- Context management
- Agent instance per session
"""
```

### mcp_server.py
```python
"""
FastMCP tool definitions.

Responsibilities:
- All MCP tool decorators
- Tool parameter schemas
- Tool callback invocation
- Result processing
"""
```

### models.py
```python
"""
Pydantic models for data validation.

Responsibilities:
- Message schemas (UserMessage, AgentResponse, etc.)
- Query DSL models (WorkflowQuery, Filter, etc.)
- Tool parameter models
- Result models
"""
```

### config.py
```python
"""
Configuration management.

Responsibilities:
- Environment variable loading
- Settings class with defaults
- WebSocket config
- LLM provider config
- Tool execution config
"""
```

### callback_router.py
```python
"""
Tool callback routing and execution.

Responsibilities:
- Callback request queue
- WebSocket message sending
- Result waiting/timeout
- Retry logic
- Error handling
"""
```

## Frontend Structure Detail

### chat_ui.js
```javascript
"""
Chat sidebar UI component.

Responsibilities:
- Render chat interface
- Message input handling
- Message history display
- Connection status indicator
- Typing indicators
- Error notifications
- Mermaid diagram rendering
"""
```

### ws_client.js
```javascript
"""
WebSocket client implementation.

Responsibilities:
- WebSocket connection management
- Automatic reconnection
- Message serialization/deserialization
- Event handlers
- Heartbeat mechanism
- Message queue for offline messages
"""
```

### session_manager.js
```javascript
"""
Session management.

Responsibilities:
- Generate/retrieve session_id
- localStorage persistence
- Session reset functionality
"""
```

### tool_executor.js
```javascript
"""
Tool execution handler.

Responsibilities:
- Route tool requests to FL_JS functions
- Parameter validation
- Function execution
- Result/error capture
- Execution logging
"""
```

### query_executor.js
```javascript
"""
Query DSL execution engine.

Responsibilities:
- Parse query objects
- Apply filters
- Graph traversal
- Aggregation
- Result formatting
"""
```

### fl_api.js
```javascript
"""
FL_JS API wrapper.

Responsibilities:
- Wrap all legacy FL_JS functions
- Provide clean interface
- Error handling
- ComfyUI API integration
"""
```

### diagram_generator.js
```javascript
"""
Mermaid diagram generation.

Responsibilities:
- Generate Mermaid syntax from nodes
- Workflow overview diagrams
- Subgraph diagrams
- Connection visualization
"""
```

## Dependencies

### Python (requirements.txt)
```txt
# Core framework
fastapi==0.104.1
uvicorn[standard]==0.24.0
python-multipart==0.0.6

# WebSocket
websockets==12.0

# AI & Tools
pydantic-ai==0.0.14
fastmcp==0.2.0
pydantic==2.5.0
pydantic-settings==2.1.0

# LLM Providers (choose one or more)
openai==1.3.0
anthropicai==0.8.0
google-generativeai==0.3.0

# Utilities
python-dotenv==1.0.0

# Testing
pytest==7.4.3
pytest-asyncio==0.21.1
pytest-cov==4.1.0
httpx==0.25.2  # For testing FastAPI
```

### JavaScript (package.json - optional)
```json
{
  "name": "fl-js-frontend",
  "version": "1.0.0",
  "description": "FL_JS Agentic System Frontend",
  "scripts": {
    "test": "jest",
    "lint": "eslint ."
  },
  "devDependencies": {
    "jest": "^29.7.0",
    "eslint": "^8.54.0"
  },
  "dependencies": {
    "mermaid": "^10.6.1"
  }
}
```

## Configuration Files

### .env.example
```bash
# LLM Provider (choose one)
LLM_PROVIDER=openai  # or anthropic, gemini
OPENAI_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
GOOGLE_API_KEY=your_key_here

# Model configuration
LLM_MODEL=gpt-4-turbo-preview
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=4000

# WebSocket settings
WS_HOST=0.0.0.0
WS_PORT=8000
WS_HEARTBEAT_INTERVAL=30
WS_SESSION_TIMEOUT=300
WS_MAX_RECONNECT_ATTEMPTS=5

# Connection limits
MAX_CONNECTIONS_PER_IP=10
MAX_MESSAGE_SIZE=1000000

# Tool execution
TOOL_TIMEOUT=30000
MAX_TOOL_RETRIES=3

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json
```

### pyproject.toml
```toml
[tool.poetry]
name = "fl-js-backend"
version = "1.0.0"
description = "FL_JS Agentic System Backend"
authors = ["Your Name <you@example.com>"]

[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.104.1"
uvicorn = {extras = ["standard"], version = "^0.24.0"}
pydantic-ai = "^0.0.14"
fastmcp = "^0.2.0"
pydantic = "^2.5.0"

[tool.poetry.dev-dependencies]
pytest = "^7.4.3"
pytest-asyncio = "^0.21.1"
pytest-cov = "^4.1.0"
ruff = "^0.1.6"
mypy = "^1.7.0"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
python_version = "3.11"
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
```

### .gitignore
```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environments
venv/
env/
ENV/
.venv/

# Environment variables
.env
.env.local

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# Testing
.pytest_cache/
.coverage
htmlcov/
.tox/

# Logs
*.log
logs/

# OS
.DS_Store
Thumbs.db

# Node (if using)
node_modules/
package-lock.json
yarn.lock

# Project specific
sessions/
*.db
*.sqlite
```

## File Creation Order

### Phase 1: Backend Foundation
1. `backend/__init__.py`
2. `backend/config.py`
3. `backend/models.py`
4. `backend/websocket.py`
5. `backend/server.py`

### Phase 2: Frontend Foundation
6. `frontend/session_manager.js`
7. `frontend/ws_client.js`
8. `frontend/chat_ui.js`

### Phase 3: Tool System
9. `backend/callback_router.py`
10. `backend/mcp_server.py`
11. `frontend/fl_api.js`
12. `frontend/tool_executor.js`

### Phase 4: Query System
13. `frontend/query_executor.js`
14. `frontend/diagram_generator.js`

### Phase 5: Agent Integration
15. `backend/agent.py`
16. `backend/utils.py`

### Phase 6: Testing
17. Create all test files
18. Set up CI/CD (optional)

## Module Responsibilities Matrix

| Module | Reads From | Writes To | Depends On |
|--------|-----------|-----------|------------|
| server.py | config.py | websocket.py | FastAPI, websocket.py |
| websocket.py | - | agent.py, callback_router.py | WebSocket, models.py |
| agent.py | config.py | mcp_server.py | PydanticAI, models.py |
| mcp_server.py | - | callback_router.py | FastMCP, models.py |
| callback_router.py | websocket.py | websocket.py | asyncio, models.py |
| ws_client.js | session_manager.js | chat_ui.js, tool_executor.js | WebSocket API |
| tool_executor.js | ws_client.js | fl_api.js | - |
| query_executor.js | - | - | - |
| fl_api.js | - | ComfyUI API | ComfyUI |
| chat_ui.js | ws_client.js | DOM | Mermaid.js |

## Integration Points

### ComfyUI Integration
1. **Loading**: Frontend files loaded as ComfyUI extension
2. **UI Injection**: Chat sidebar injected into ComfyUI DOM
3. **API Access**: Access to `app.graph`, `app.canvas`, etc.
4. **Event Hooks**: Listen to ComfyUI execution events

### Backend-Frontend Communication
1. **WebSocket**: Primary communication channel
2. **Session ID**: All messages tagged with session_id
3. **Message Protocol**: Defined in models.py, implemented in both
4. **Tool Callbacks**: Backend → Frontend via WebSocket

### LLM Provider Integration
1. **PydanticAI**: Abstracts LLM provider
2. **Config**: Provider selected via environment variable
3. **Tools**: MCP tools registered with agent
4. **Context**: Conversation history maintained per session

## Development Workflow

### Local Development Setup
```bash
# Backend
cd backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
uvicorn server:app --reload --port 8000

# Frontend (in ComfyUI)
# Copy frontend files to ComfyUI/web/extensions/fl_js/
# Restart ComfyUI
```

### Testing
```bash
# Backend tests
pytest tests/backend/ -v --cov=backend

# Frontend tests (if using Jest)
npm test

# Integration tests
pytest tests/integration/ -v
```

### Code Quality
```bash
# Linting
ruff check backend/
eslint frontend/

# Type checking
mypy backend/

# Formatting
ruff format backend/
```

## Deployment Considerations

### Backend Deployment
- **Container**: Docker image with Python app
- **Process Manager**: Uvicorn with multiple workers
- **Reverse Proxy**: Nginx for WebSocket support
- **SSL**: Required for secure WebSocket (wss://)

### Frontend Deployment
- **Static Files**: Served from ComfyUI web directory
- **CDN**: Optional for external dependencies (Mermaid.js)
- **Minification**: Optional for production

### Database (Future)
- **Session Persistence**: Redis or PostgreSQL
- **Conversation History**: PostgreSQL
- **Analytics**: Separate analytics DB

## Summary

✅ **Clear separation** of backend and frontend
✅ **Modular design** for easy maintenance
✅ **Well-defined interfaces** between components
✅ **Comprehensive testing** structure
✅ **Development workflow** established
✅ **Deployment ready** architecture
