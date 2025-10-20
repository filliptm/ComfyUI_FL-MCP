# FL_JS Agentic System 🤖

> **AI-Powered ComfyUI Workflow Assistant** - Create, modify, and understand ComfyUI workflows through natural language conversation.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Extension-orange.svg)](https://github.com/comfyanonymous/ComfyUI)

---

## ✨ Features

### 🎯 Natural Language Workflow Control
- **Chat with your workflow** - "Create a txt2img workflow with SDXL"
- **Modify on the fly** - "Change all KSampler steps to 30"
- **Query your graph** - "Show me all nodes connected to the checkpoint loader"
- **Visual feedback** - Get Mermaid diagrams of your workflow structure

### 🛠️ Comprehensive Tool Suite (40+ Tools)
- **Node Management** - Create, find, remove, bypass, pin, and select nodes
- **Node Manipulation** - Get/set parameters, connect nodes intelligently
- **Layout Control** - Auto-arrange workflows, position nodes relative to each other
- **Workflow Execution** - Queue, cancel, batch processing, monitor status
- **Advanced Queries** - Filter nodes, traverse connections, aggregate data

### 🧠 Intelligent Agent
- **Context-aware** - Remembers conversation history and workflow state
- **Proactive suggestions** - Warns about disconnected nodes, suggests improvements
- **Best practices** - Knows ComfyUI patterns and common workflow structures
- **Multi-LLM support** - Works with OpenAI, Anthropic Claude, Google Gemini, or OpenRouter

### 🎨 Native ComfyUI Integration
- **Sidebar panel** - Seamlessly integrated into ComfyUI's left drawer
- **Dark theme** - Matches ComfyUI's aesthetic perfectly
- **Real-time updates** - WebSocket-based instant communication
- **Multi-session** - Each browser tab gets its own isolated agent
- **🆕 Auto-start backend** - No manual server startup required!

---

## 🚀 Quick Start

### Prerequisites
- **ComfyUI** installed and working
- **Python 3.11+** for the backend server
- **API Key** for your chosen LLM provider (OpenAI, Anthropic, Google, or OpenRouter)

### Installation

#### 1. Clone into ComfyUI custom_nodes directory
```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/yourusername/fl_js.git FL_JS
cd FL_JS
```

> **Important:** The directory must be named `FL_JS` (or your preferred name) inside `custom_nodes/`

#### 2. Install Python dependencies
```bash
pip install -r requirements.txt
```

> **Note:** Use ComfyUI's Python environment if you have a custom setup.

#### 3. Configure your LLM provider

Copy the example environment file:
```bash
cp .env.example .env
```

Edit `.env` with your settings:

```bash
# Choose your provider: openai, anthropic, gemini, or openrouter
LLM_PROVIDER=gemini

# Add your API key for the chosen provider
GOOGLE_API_KEY=your-key-here

# Model name is OPTIONAL - intelligent defaults are used
# Leave blank to use the recommended default for your provider
LLM_MODEL=
```

> **💡 Smart Defaults:** If you don't specify `LLM_MODEL`, the system automatically uses the best model for your provider. See [Provider Configuration](#-provider-configuration) below for details.

#### 4. Start ComfyUI

🎉 **That's it!** The backend starts automatically when ComfyUI loads:

```bash
cd /path/to/ComfyUI
python main.py
```

You should see:
```
================================================================================
[FL_JS] Initializing FL_JS Agentic System...
================================================================================
[FL_JS] Starting backend server on port 8000...
[FL_JS] Waiting for backend to be ready... Ready!
[FL_JS] Backend server started successfully! (PID: 12345)
[FL_JS] Auto-restart monitoring enabled
================================================================================
[FL_JS] Initialization complete!
================================================================================
```

> **Note:** Backend logs are saved to `backend/logs/server.log`

#### 5. Verify installation

Open ComfyUI in your browser and check the **browser console** (F12):

```
[FL_JS] Extension module loaded
[FL_JS] Initializing Agentic System extension...
[FL_JS] Session ID: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
[FL_JS] Connecting to backend server...
[FL_JS] WebSocket connected
[FL_JS] Handshake complete: connected
[FL_JS] Extension initialized successfully!
```

If you see these messages, **you're ready to go!** 🎉

---

## 🔌 Provider Configuration

### Intelligent Provider Defaults

FL_JS uses **smart defaults** for each provider. You only need to set `LLM_PROVIDER` and the corresponding API key - the best model is chosen automatically:

| Provider | Default Model | API Key Required |
|----------|---------------|------------------|
| **OpenAI** | `gpt-4-turbo-preview` | `OPENAI_API_KEY` |
| **Anthropic** | `claude-3-5-sonnet-20241022` | `ANTHROPIC_API_KEY` |
| **Gemini** | `gemini-2.0-flash-exp` | `GOOGLE_API_KEY` |
| **OpenRouter** | `deepseek/deepseek-chat` | `OPENROUTER_API_KEY` |

### Quick Provider Switch

Switching providers is as simple as changing two lines in `.env`:

```bash
# Switch to Gemini
LLM_PROVIDER=gemini
GOOGLE_API_KEY=your-google-key

# Switch to Claude
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-key

# Switch to OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-key

# Switch to OpenRouter (access 100+ models)
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=your-openrouter-key
```

Restart ComfyUI and you're using the new provider! 🚀

### Model Override (Optional)

Want to use a specific model? Just set `LLM_MODEL` in `.env`:

```bash
# Use GPT-4 instead of default
LLM_PROVIDER=openai
LLM_MODEL=gpt-4

# Use Claude Opus instead of Sonnet
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-opus-20240229

# Use Gemini 1.5 Pro instead of 2.0 Flash
LLM_PROVIDER=gemini
LLM_MODEL=gemini-1.5-pro

# Use any model through OpenRouter
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-3.5-sonnet
```

### Provider-Specific Optimizations

FL_JS automatically tunes behavior for each provider:

- **Gemini**: Reduced message history (16 vs 36), compressed tool results for better JSON generation
- **Anthropic**: Extended context window (50 messages, 4000 chars per result)
- **OpenAI**: Balanced settings (36 messages, 2000 chars per result)
- **OpenRouter**: Same as OpenAI defaults

These optimizations happen **automatically** - no configuration needed!

### Validation & Safety

The system validates your provider/model combination:

```python
# ✅ Valid combinations
LLM_PROVIDER=openai + LLM_MODEL=gpt-4
LLM_PROVIDER=anthropic + LLM_MODEL=claude-3-sonnet-20240229
LLM_PROVIDER=gemini + LLM_MODEL=gemini-2.0-flash-exp

# ❌ Invalid combinations (caught at startup)
LLM_PROVIDER=openai + LLM_MODEL=claude-3-opus  # Wrong model for provider
LLM_PROVIDER=gemini + LLM_MODEL=gpt-4  # Wrong model for provider
```

Invalid combinations are caught immediately with helpful error messages.

### Supported Models

#### OpenAI
- `gpt-4-turbo-preview` ⭐ (Default)
- `gpt-4`
- `gpt-3.5-turbo`
- `o1-preview`, `o1-mini`

#### Anthropic
- `claude-3-5-sonnet-20241022` ⭐ (Default)
- `claude-3-opus-20240229`
- `claude-3-sonnet-20240229`
- `claude-3-haiku-20240307`

#### Google Gemini
- `gemini-2.0-flash-exp` ⭐ (Default)
- `gemini-1.5-pro`
- `gemini-1.5-flash`
- `models/gemini-pro` (legacy format)

#### OpenRouter
- `deepseek/deepseek-chat` ⭐ (Default)
- Access to 100+ models from multiple providers
- Use format: `provider/model-name`
- Examples: `anthropic/claude-3.5-sonnet`, `google/gemini-pro-1.5`, `meta-llama/llama-3.1-70b`

---

## ⚙️ Backend Configuration

### Auto-Start Settings

The backend automatically starts when ComfyUI loads. You can customize this behavior in `.env`:

```bash
# === Backend Auto-Start ===
AUTO_START_BACKEND=true          # Enable/disable auto-start
AUTO_RESTART_BACKEND=true        # Auto-restart if backend crashes
LOG_BACKEND_TO_FILE=true         # Log to backend/logs/server.log
WS_PORT=8000                     # Backend server port
```

### Manual Backend Start (Optional)

If you prefer to start the backend manually:

1. **Disable auto-start** in `.env`:
   ```bash
   AUTO_START_BACKEND=false
   ```

2. **Start manually** in a separate terminal:
   ```bash
   cd backend
   python server.py
   ```

3. **Restart ComfyUI** to load the extension

### Port Configuration

If port 8000 is already in use:

1. **Change the port** in `.env`:
   ```bash
   WS_PORT=8001
   ```

2. **Restart ComfyUI** (backend will use new port)

---

## 💬 Usage Examples

### Creating Workflows
```
You: "Create a simple text-to-image workflow"

Agent: "I'll create a basic txt2img workflow for you."
       [Creates and connects: CheckpointLoader → CLIPTextEncode (positive/negative) 
        → EmptyLatentImage → KSampler → VAEDecode → SaveImage]
       "Done! I've created a complete workflow with 7 nodes."
```

### Modifying Workflows
```
You: "Change the sampler to use 40 steps with euler_ancestral"

Agent: "I'll update the KSampler settings."
       [Finds KSampler node, sets steps=40, sampler_name="euler_ancestral"]
       "Updated! The sampler now uses 40 steps with euler_ancestral."
```

### Querying Workflows
```
You: "Show me all LoRA nodes and their weights"

Agent: [Queries workflow, finds LoRA loaders]
       "Found 2 LoRA nodes:
        - Node #5: 'detail_enhancer.safetensors' (weight: 0.8)
        - Node #12: 'style_helper.safetensors' (weight: 0.6)"
```

### Visual Diagrams
```
You: "Show me the workflow structure"

Agent: [Generates Mermaid diagram]
       ```mermaid
       graph LR
         N1[CheckpointLoader] --> N2[CLIPTextEncode]
         N1 --> N3[KSampler]
         N2 --> N3
         ...
       ```
```

---

## 🏗️ Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     ComfyUI Browser                         │
│  ┌────────────────┐              ┌────────────────────┐    │
│  │  Chat Sidebar  │◄──────────────┤   FL_JS Legacy     │    │
│  │  (extension.js)│  Tool Calls  │   (fl_js.js)       │    │
│  └────────┬───────┘              └────────────────────┘    │
│           │ WebSocket                                       │
└───────────┼─────────────────────────────────────────────────┘
            │
            │ ws://localhost:8000/ws
            │
┌───────────▼─────────────────────────────────────────────────┐
│                    Backend Server (Python)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │  WebSocket   │  │  PydanticAI  │  │   FastMCP       │  │
│  │  Manager     │──┤    Agent     │──┤   Tools (40+)   │  │
│  └──────────────┘  └──────────────┘  └─────────────────┘  │
│         │                  │                    │           │
│         └──────────────────┴────────────────────┘           │
│                     Session Management                      │
│              (Isolated per browser tab)                     │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              ServerRunner (Auto-Start)               │  │
│  │  • Subprocess management                             │  │
│  │  • Port conflict detection                           │  │
│  │  • Auto-restart on crash                             │  │
│  │  • Dual logging (file + stdout)                      │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

#### Frontend (JavaScript)
- **`web/js/extension.js`** - ComfyUI extension entry point
- **`web/js/session_manager.js`** - Session management with localStorage
- **`web/js/ws_client.js`** - WebSocket client with auto-reconnection
- **`web/js/chat_ui.js`** - Chat interface (Phase 4)
- **`web/js/tool_executor.js`** - Tool execution (Phase 2)
- **`web/js/fl_api.js`** - FL_JS API wrapper (Phase 2)
- **`web/js/query_executor.js`** - Query DSL executor (Phase 3)
- **`web/js/diagram_generator.js`** - Mermaid diagrams (Phase 4)

#### Backend (Python)
- **`backend/server.py`** - FastAPI application with WebSocket endpoint
- **`backend/server_runner.py`** - 🆕 Subprocess manager for auto-start
- **`backend/websocket.py`** - Connection manager, session routing
- **`backend/config.py`** - Configuration and settings
- **`backend/model_defaults.py`** - 🆕 Provider defaults and validation
- **`backend/models.py`** - Pydantic models for messages and queries
- **`backend/agent.py`** - PydanticAI agent (Phase 3)
- **`backend/mcp_server.py`** - FastMCP tool definitions (Phase 2)
- **`backend/callback_router.py`** - Tool callback routing (Phase 2)
- **`backend/utils.py`** - Utility functions (Phase 3)

---

## 🔧 Configuration

### Environment Variables

All configuration is in `.env` (copy from `.env.example`):

```bash
# === Backend Auto-Start ===
AUTO_START_BACKEND=true          # Auto-start backend when ComfyUI loads
AUTO_RESTART_BACKEND=true        # Auto-restart on crash
LOG_BACKEND_TO_FILE=true         # Log to backend/logs/server.log

# === LLM Provider ===
LLM_PROVIDER=gemini              # openai, anthropic, gemini, or openrouter
LLM_MODEL=                       # Optional - uses smart defaults if blank
LLM_TEMPERATURE=0.7              # Creativity (0.0-1.0)
LLM_MAX_TOKENS=32000             # Max response length

# === API Keys ===
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
OPENROUTER_API_KEY=sk-or-...

# === WebSocket Settings ===
WS_HOST=0.0.0.0                  # Server host
WS_PORT=8000                     # Server port
WS_HEARTBEAT_INTERVAL=30         # Seconds between heartbeats
WS_SESSION_TIMEOUT=300           # Session timeout (seconds)
WS_MAX_RECONNECT_ATTEMPTS=5      # Max reconnection attempts

# === Tool Execution ===
TOOL_TIMEOUT=30000               # Tool timeout (milliseconds)
MAX_TOOL_RETRIES=3               # Max retries on tool failure

# === Conversation ===
CONVERSATION_MAX_HISTORY=50      # Max messages to remember

# === Logging ===
LOG_LEVEL=INFO                   # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT=json                  # json or text
```

---

## 🧪 Development

### Project Structure

```
FL_JS/
├── __init__.py           # ComfyUI custom node registration + auto-start
├── backend/              # Python FastAPI server
│   ├── __init__.py
│   ├── server.py         # Main FastAPI app
│   ├── server_runner.py  # 🆕 Subprocess manager
│   ├── websocket.py      # WebSocket connection manager
│   ├── config.py         # Configuration
│   ├── model_defaults.py # 🆕 Provider defaults
│   ├── models.py         # Pydantic models
│   ├── agent.py          # PydanticAI agent (Phase 3)
│   ├── mcp_server.py     # FastMCP tools (Phase 2)
│   ├── callback_router.py # Tool callbacks (Phase 2)
│   ├── utils.py          # Utilities (Phase 3)
│   └── logs/             # 🆕 Backend logs
│       └── server.log    # Auto-generated
│
├── web/                  # JavaScript extensions
│   └── js/
│       ├── extension.js      # ComfyUI extension entry
│       ├── session_manager.js # Session management
│       ├── ws_client.js      # WebSocket client
│       ├── chat_ui.js        # Chat UI (Phase 4)
│       ├── tool_executor.js  # Tool execution (Phase 2)
│       ├── query_executor.js # Query DSL (Phase 3)
│       ├── fl_api.js         # FL_JS wrapper (Phase 2)
│       └── diagram_generator.js # Mermaid (Phase 4)
│
├── legacy/               # Original FL_JS code
│   ├── FL_JS.py          # Original node
│   ├── fl_js.js          # Original functions
│   └── NodePackLoader_SideBar.js # Reference
│
├── tests/                # Test suites
│   ├── backend/          # Backend tests
│   ├── frontend/         # Frontend tests
│   └── integration/      # E2E tests
│
├── notes/                # Documentation & plans
│   ├── implementation/   # Implementation plans
│   └── comfy_init/       # 🆕 Auto-start research
│
├── .env.example          # Environment template
├── requirements.txt      # Python dependencies
├── pyproject.toml        # Python project config
└── README.md             # This file
```

### Running Tests

```bash
# Backend tests
cd backend
pytest tests/backend/ -v --cov=backend

# Integration tests
pytest tests/integration/ -v

# With coverage report
pytest --cov=backend --cov-report=html
```

### Code Quality

```bash
# Linting
ruff check backend/

# Type checking
mypy backend/

# Formatting
ruff format backend/
```

---

## 📚 Advanced Topics

### Query DSL

The agent uses a JSON-based query language to find and analyze nodes:

```javascript
// Find all KSampler nodes
{
  "filters": {
    "operator": "and",
    "filters": [
      {"field": "type", "operator": "equals", "value": "KSampler"}
    ]
  }
}

// Find checkpoint loaders with specific model
{
  "filters": {
    "operator": "and",
    "filters": [
      {"field": "type", "operator": "equals", "value": "CheckpointLoaderSimple"},
      {"field": "parameters.ckpt_name", "operator": "contains", "value": "sdxl"}
    ]
  }
}

// Traverse downstream from a node
{
  "filters": {
    "operator": "and",
    "filters": [{"field": "id", "operator": "equals", "value": 5}]
  },
  "traversal": {
    "direction": "downstream",
    "max_depth": null  // unlimited
  }
}
```

See `notes/implementation/02_query_dsl.md` for complete documentation.

### Multi-Session Support

Each browser tab gets its own isolated session:
- Unique `session_id` stored in localStorage
- Separate agent instance with independent conversation history
- No message mixing between tabs
- Automatic session cleanup after timeout

### Tool Callback Flow

1. Agent decides to use a tool (e.g., "create_node")
2. Backend sends tool request to frontend via WebSocket
3. Frontend executes FL_JS function
4. Frontend sends result back via WebSocket
5. Backend returns result to agent
6. Agent continues with response

All async, all non-blocking! ⚡

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Workflow

1. Read the implementation plans in `notes/implementation/`
2. Check `notes/implementation/progress.md` for current status
3. Pick a task from the roadmap
4. Write tests first (TDD)
5. Implement the feature
6. Update documentation
7. Submit PR

---

## 📝 License

MIT License - see [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **ComfyUI** - The amazing node-based UI for Stable Diffusion
- **PydanticAI** - Modern Python agent framework
- **FastMCP** - Model Context Protocol implementation
- **Mermaid.js** - Beautiful diagram rendering
- Original **FL_JS** - The foundation this builds upon
- **ComfyUI-NODEJS** - Inspiration for auto-start implementation

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/fl_js/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/fl_js/discussions)
- **Documentation**: See `notes/implementation/` for detailed docs

---

## 🗺️ Roadmap

See `notes/implementation/progress.md` for current status. Highlights:

- ✅ **Phase 1**: Backend & frontend foundation (COMPLETE)
- ✅ **Phase 1.5**: ComfyUI integration (COMPLETE)
- ✅ **Phase 1.75**: Backend auto-start (COMPLETE)
- 🚧 **Phase 2**: Tool system (40+ MCP tools)
- 📋 **Phase 3**: Query DSL & agent
- 📋 **Phase 4**: Chat UI & integration
- 📋 **Phase 5**: Polish & testing

### Future Features
- 🚧 Streaming responses
- 📋 Workflow templates library
- 📋 Execution monitoring & feedback loop
- 📋 Plugin system for custom tools
- 📋 Workflow version control
- 📋 Collaborative editing

---

## 🐛 Troubleshooting

### Backend doesn't start automatically

**Check ComfyUI console for errors:**

```
[FL_JS] Port 8000 already in use.
```
**Solution:** Change `WS_PORT` in `.env` or stop the conflicting service.

```
[FL_JS] Error: server.py not found
```
**Solution:** Reinstall or check file permissions.

```
[FL_JS] Backend server failed to start (timeout)
```
**Solution:** Check `backend/logs/server.log` for detailed errors.

### Backend keeps restarting

**Check logs:**
```bash
tail -f backend/logs/server.log
```

**Common causes:**
- Missing API key in `.env`
- Invalid model name
- Port conflict
- Missing dependencies

**Disable auto-restart temporarily:**
```bash
# In .env
AUTO_RESTART_BACKEND=false
```

### Extension doesn't load

**Check browser console (F12):**
- Should see `[FL_JS] Extension module loaded`
- If not, check ComfyUI terminal for errors
- Verify `__init__.py` exists at project root
- Verify `WEB_DIRECTORY = "./web/js"` in `__init__.py`

### WebSocket connection fails

**Check backend status:**
```bash
# Check if backend is running
lsof -i :8000  # Linux/Mac
netstat -ano | findstr :8000  # Windows
```

**Check console for errors:**
- `[WSClient] Connection error` - Backend not running
- `[WSClient] Max reconnection attempts reached` - Backend unreachable

**Verify WebSocket URL:**
- Default: `ws://localhost:8000/ws`
- Check `web/js/extension.js` line 23

### Session ID mismatch

**Clear localStorage:**
```javascript
// In browser console:
localStorage.removeItem('fl_js_session_id');
location.reload();
```

### Manual backend control

**Stop auto-start:**
```bash
# In .env
AUTO_START_BACKEND=false
```

**Find and kill backend process:**
```bash
# Linux/Mac
lsof -i :8000
kill <PID>

# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F
```

**View backend logs:**
```bash
tail -f backend/logs/server.log
```

---

**Built with ❤️ for the ComfyUI community**
