# FL_JS Project Structure

## Overview
A ComfyUI custom node extension that adds JavaScript execution and AI-powered workflow automation capabilities.

## Files

### Core Nodes

#### `FL_JS.py` / `fl_js.js`
- **Purpose**: Basic JavaScript event handler node for ComfyUI
- **Category**: 🏵️Fill Nodes/Utility
- **Type**: Event-driven execution node
- **Key Features**:
  - Executes JS code on workflow lifecycle events
  - Provides comprehensive workflow manipulation API
  - No return types (utility/side-effect node)

#### `FL_WF_Agent.py` / `fl_wf_agent.js`
- **Purpose**: AI-enhanced workflow automation with Gemini integration
- **Category**: 🏵️Fill Nodes/WIP
- **Type**: AI code generator + executor
- **Key Features**:
  - Natural language to JavaScript code generation
  - Node scanning/cataloging system
  - Gemini API integration (gemini-2.0-flash model)
  - Built on top of FL_JS functionality

## Architecture

### Event System
Both nodes hook into ComfyUI's event lifecycle:
- `before_queued` - Before prompt is queued
- `after_queued` - After prompt is queued
- `status` - Status updates
- `progress` - Progress updates
- `executing` - Node execution events
- `executed` - After node execution
- `execution_start` - Workflow execution start
- `execution_success` - Workflow success
- `execution_error` - Workflow errors
- `execution_cached` - Cached execution

### JavaScript API

#### Node Management
```javascript
find(ID|TITLE|TYPE) => Node
findLast(ID|TITLE|TYPE) => Node
create(TYPE, {key: value, ...}) => Node
remove(...NODE)
bypass(...NODE)
unbypass(...NODE)
pin(...NODE)
unpin(...NODE)
select(...NODE)
```

#### Node Manipulation
```javascript
getValues(NODE) => {key: value, ...}
setValues(NODE, {key: value, ...})
connect(OUTPUT_NODE, OUTPUT_SLOT_NAME, INPUT_NODE, INPUT_SLOT_NAME?)
```

#### Layout Functions
```javascript
putOnLeft(NODE, TARGET_NODE)
putOnRight(NODE, TARGET_NODE)
putOnTop(NODE, TARGET_NODE)
putOnBottom(NODE, TARGET_NODE)
moveToRight(NODE)
moveToBottom(NODE)
getRect(NODE) => [x, y, width, height]
setRect(NODE, x?, y?, width?, height?)
```

#### Workflow Control
```javascript
generate() // Start generation
cancel() // Cancel current generation
enableAutoQueue()
disableAutoQueue()
setBatchCount(number)
```

#### System Control
```javascript
disableSleep()
enableSleep()
disableScreenSaver()
enableScreenSaver()
sendImages(url, field, ...PREVIEW_NODE)
```

#### Utilities
```javascript
generateSeed() => number
generateFloat(min, max) => number
generateInt(min, max) => number
random(...any[]) => any
```

### Context Variables
Each execution provides:
- `SELF` - Current node
- `COMMAND` - JavaScript code to execute
- `STATE` - Persistent state (node.__eventHandler__)
- `PROPS` - Persistent properties (node.properties.__eventHandler__)
- `NODES` - All graph nodes
- `LINKS` - All graph links
- `ARGS` - Event arguments
- `DATE`, `YEAR`, `MONTH`, `DAY`, `HOURS`, `MINUTES`, `SECONDS` - Time info
- `BATCH_COUNT`, `QUEUE_MODE`, `AUTO_QUEUE` - Workflow settings

## FL_WF_Agent Specifics

### Node Scanner
- Located at: `../scanner.py` (relative to FL_WF_Agent.py)
- Scans ComfyUI nodes and creates cache file
- Cache location: `web/nodes/node_definitions.txt`
- Provides node definitions to Gemini for context-aware code generation

### Gemini Integration
- **API**: Google Generative Language API
- **Model**: gemini-2.0-flash
- **Endpoint**: `https://generativelanguage.googleapis.com/v1/models/{model}:generateContent`
- **System Prompt**: Instructs Gemini to generate raw JavaScript code (no markdown)
- **Context**: Includes node definitions from scanner cache

### Code Generation Flow
1. User enters natural language prompt
2. Scanner loads available node definitions
3. System prompt + node definitions + user prompt sent to Gemini
4. Response sanitized (removes markdown, quotes, etc.)
5. Generated code placed in javascript widget
6. User can execute generated code

### Inputs
- `event` - Lifecycle event trigger
- `code_prompt` - Natural language prompt for code generation
- `api_key` - Gemini API key
- `javascript` - Generated/editable JavaScript code
- `scan_nodes` (optional) - Trigger node scanning

### UI Widgets
- 🤖 Generate Code - Calls Gemini API
- 🔍 Scan Nodes - Triggers node scanner
- Execute - Runs the JavaScript code

## Design Patterns

### Defensive Execution
- Try-catch wrapping in `execNode()`
- Error logging to console
- Graceful degradation when node definitions unavailable

### State Management
- `__eventHandler__` for transient state
- `properties.__eventHandler__` for persistent properties
- Both scoped per-node

### Collision Detection
- `moveToRight()` and `moveToBottom()` use iterative collision detection
- Prevents node overlap during automated layout

### API Design
- Flexible node finding (by ID, title, or type)
- Smart connection matching (case-insensitive, type-based fallback)
- Optional parameters with sensible defaults

## Dependencies

### Python Side
- ComfyUI custom node API
- Scanner module (for FL_WF_Agent)
- subprocess (for scanner execution)

### JavaScript Side
- ComfyUI app.js and api.js
- LiteGraph (node graph library)
- Fetch API (for Gemini calls)

## Constants

```javascript
MIN_SEED = 0
MAX_SEED = 0xffffffffffffffff
STEPS_OF_SEED = 10
DEFAULT_MARGIN_X = 32
DEFAULT_MARGIN_Y = 64
GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com'
GEMINI_API_VERSION = 'v1'
GEMINI_MODEL = 'gemini-2.0-flash'
```

## Usage Examples

### Basic Node Creation
```javascript
const checkpoint = create("CheckpointLoaderSimple", {
    ckpt_name: "v1-5-pruned.ckpt"
});
```

### Node Connection
```javascript
connect(checkpoint, "CLIP", clipPos, "clip");
```

### Animated Workflow
```javascript
async function buildWorkflow() {
    const nodes = [];
    
    const node1 = create("SomeNode", {});
    nodes.push(node1);
    await sleep(100);
    
    const node2 = create("AnotherNode", {});
    connect(node1, "OUTPUT", node2, "input");
    nodes.push(node2);
    await sleep(100);
    
    generate();
}
```

## Notes

- Both nodes register after 1024ms delay to ensure ComfyUI is ready
- FL_WF_Agent attempts multiple paths to load node definitions
- Code sanitization removes markdown formatting from Gemini responses
- Scanner runs as subprocess for isolation
- Cache file created at: `custom_nodes/ComfyUI_Fill-Nodes/web/nodes/node_definitions.txt`
