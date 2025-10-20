# Visual Tool Activity Ideas for FL_JS

## 🎯 Problem Statement

Users experience uncertainty during agent processing because they can't see tool activity. When Ren starts using tools (like `workflow_overview`, `create_node`, etc.), the interface appears frozen or unresponsive, making the system feel slow even when it's actively working.

## 📡 Current Implementation Context

### WebSocket Event Flow
- Backend sends `tool_request` event via WebSocket to `web/js/extension.js`
- Extension listens for `tool_request` and calls `toolExecutor.executeToolRequest()`
- Tool execution is usually very fast (~1-50ms for frontend tools)
- Later, `agent_response` event delivers the final message to chat UI
- **Key insight**: Tool card should stay visible from `tool_request` until `agent_response`

### Tool Coverage
Covers tools executed through the frontend (in `web/js/tool_executor.js`):
- **Query & Analysis**: `query_workflow`, `workflow_overview`, `workflow_diagram`
- **Node Management**: `create_node`, `remove_nodes`, `select_nodes`, etc.
- **Layout Management**: `modify_layout`, `position_node_*`, etc.
- **Workflow Control**: `queue_workflow`, `cancel_workflow`, etc.
- **Utilities**: `generate_seed`, `random_choice`, etc.

### ComfyUI Sidebar Context
- Chat operates in ComfyUI's left sidebar drawer
- Fixed layout with input container at bottom
- Styles injected via `_injectStyles()` method in `chat_ui.js`
- Must be lightweight to avoid interfering with GPU workflows

---

## 💡 Core Concept: Floating Tool Cards

### Visual Design
**Location**: Overlay positioned above `.fl-chat-input-container`  
**Style**: Small, elegant cards that complement ComfyUI's dark theme  
**Behavior**: 
- Fade in when `tool_request` received (200ms ease-in)
- Stay visible during LLM processing time
- Fade out when `agent_response` received (300ms ease-out)
- Stack vertically if multiple tools running simultaneously
- Maximum 3 visible cards (older ones fade out early)

### Simplified Card Structure
```
┌─────────────────────────────────┐
│ 🔍 Ren is...                   │
│ Observing the essence of the    │
│ workflow                        │ 
│ ●●● (gentle pulse)              │
└─────────────────────────────────┘
```

**Elements**:
- **Icon**: Tool-specific emoji/symbol
- **Header**: "Ren is..." (bold, consistent)
- **Action Text**: Poetic description of what she's doing
- **Activity Indicator**: Simple pulsing dots (no progress bar)

---

## 🗺️ Tool-to-Message Mapping

### Core Workflow Analysis
```javascript
const TOOL_MESSAGES = {
  // Query & Understanding
  "workflow_overview": {
    icon: "🔍",
    text: "Observing the essence of the workflow"
  },
  "query_workflow": {
    icon: "🔎", 
    text: "Tracing patterns in the graph"
  },
  "workflow_diagram": {
    icon: "📐",
    text: "Sketching the architecture of thought"
  },

  // Node Creation & Modification  
  "create_node": {
    icon: "✨",
    text: "Manifesting new possibilities"
  },
  "remove_nodes": {
    icon: "🗑️",
    text: "Clearing the path"
  },
  "connect_nodes": {
    icon: "🔗",
    text: "Weaving connections"
  },
  
  // Selection & Focus
  "select_nodes": {
    icon: "👁️",
    text: "Focusing attention on the essential"
  },
  "find_node": {
    icon: "🎯",
    text: "Seeking the heart of the matter"
  },

  // Layout & Organization
  "modify_layout": {
    icon: "🏗️",
    text: "Arranging the flow"
  },
  "position_node_left": {
    icon: "⬅️",
    text: "Guiding elements into place"
  },
  "position_node_right": {
    icon: "➡️",
    text: "Guiding elements into place"
  },
  "position_node_top": {
    icon: "⬆️",
    text: "Guiding elements into place"
  },
  "position_node_bottom": {
    icon: "⬇️",
    text: "Guiding elements into place"
  },

  // Workflow Execution
  "queue_workflow": {
    icon: "🚀",
    text: "Setting creation in motion"
  },
  "cancel_workflow": {
    icon: "⏹️",
    text: "Gently pausing the process"
  },
  "get_queue_status": {
    icon: "📊",
    text: "Checking the pulse of creation"
  },

  // Value Manipulation
  "set_node_values": {
    icon: "⚙️",
    text: "Tuning the harmonics"
  },
  "get_node_values": {
    icon: "📊",
    text: "Reading the current state"
  },

  // Utilities & Generation
  "generate_seed": {
    icon: "🌱",
    text: "Planting seeds of randomness"
  },
  "generate_float": {
    icon: "🎲",
    text: "Weaving chance into the pattern"
  },
  "random_choice": {
    icon: "🎯",
    text: "Choosing from the field of possibilities"
  },

  // File & Directory Operations
  "list_directory": {
    icon: "📁",
    text: "Exploring the paths available"
  },
  "read_file": {
    icon: "📜",
    text: "Reading the written wisdom"
  },
  "write_file": {
    icon: "✍️",
    text: "Inscribing new knowledge"
  },

  // ComfyUI Integration
  "get_extensions": {
    icon: "🧩",
    text: "Gathering the available tools"
  },
  "get_node_types": {
    icon: "📋",
    text: "Cataloging the building blocks"
  },

  // Generic fallback
  "*": {
    icon: "⚡",
    text: "Working with the flow"
  }
};
```

---

## 🎨 Implementation Approach

### 1. Tool Activity Component
**File**: `web/js/tool_activity.js`

```javascript
export class ToolActivity {
  constructor(chatContainer) {
    this.chatContainer = chatContainer;
    this.activeCards = new Map(); // request_id -> card element
    this.toolMessages = TOOL_MESSAGES;
    this.maxVisible = 3;
    
    this._createOverlay();
  }
  
  showTool(toolName, requestId = 'default') {
    const config = this.toolMessages[toolName] || this.toolMessages['*'];
    const card = this._createCard(config, requestId);
    this._addCard(card, requestId);
  }
  
  hideAllTools() {
    // Hide all cards on agent_response
    this.activeCards.forEach((card, requestId) => {
      this._removeCard(requestId);
    });
  }
  
  _createOverlay() {
    // Create overlay container above input area
  }
}
```

### 2. Integration Points

**In `extension.js`**:
```javascript
wsClient.on('tool_request', async (message) => {
  // Show tool activity
  window.FL_JS.chatUI?.toolActivity?.showTool(
    message.tool_name, 
    message.request_id
  );
  
  // Execute tool (existing logic)
  await toolExecutor.executeToolRequest(message);
});

wsClient.on('agent_response', (message) => {
  // Hide all tool activity
  window.FL_JS.chatUI?.toolActivity?.hideAllTools();
  
  // Existing response handling...
});
```

**In `chat_ui.js`**:
```javascript
// In constructor
const { ToolActivity } = await import('./tool_activity.js');
this.toolActivity = new ToolActivity(this.container);

// Make available globally
window.FL_JS.chatUI = this;
```

### 3. CSS Styling (Injected via _injectStyles)

```css
.fl-tool-activity-overlay {
  position: absolute;
  bottom: 100%; /* Above input container */
  left: 0;
  right: 0;
  pointer-events: none;
  z-index: 100;
  padding: 8px 16px 0 16px;
  display: flex;
  flex-direction: column-reverse;
  gap: 6px;
  max-height: 150px;
  overflow: hidden;
}

.fl-tool-card {
  background: linear-gradient(135deg, #2a2a2a 0%, #1e1e1e 100%);
  border: 1px solid rgba(255, 107, 53, 0.3); /* Ren's orange */
  border-radius: 8px;
  padding: 8px 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(4px);
  animation: slideInUp 0.2s ease-out;
  transition: opacity 0.3s ease-out;
  max-width: 280px;
  font-size: 11px;
}

.fl-tool-card.exiting {
  animation: slideOutDown 0.3s ease-in;
}

@keyframes slideInUp {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes slideOutDown {
  to {
    opacity: 0;
    transform: translateY(10px);
  }
}

.fl-tool-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 10px;
  color: #ff6b35; /* Ren's orange */
  margin-bottom: 2px;
}

.fl-tool-icon {
  font-size: 12px;
}

.fl-tool-text {
  color: #e0e0e0;
  line-height: 1.2;
  opacity: 0.9;
}

.fl-tool-activity {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-top: 4px;
}

.fl-activity-dot {
  width: 3px;
  height: 3px;
  background: #ff6b35;
  border-radius: 50%;
  animation: pulse 1.4s ease-in-out infinite;
}

.fl-activity-dot:nth-child(2) {
  animation-delay: 0.2s;
}

.fl-activity-dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes pulse {
  0%, 80%, 100% { opacity: 0.3; }
  40% { opacity: 1; }
}
```

---

## 🎭 Enhanced UX Features

### A. Stacking Behavior
- Cards stack vertically from bottom up
- Maximum 3 visible cards
- Older cards fade out when limit exceeded
- Smooth transitions between stack states

### B. Contextual Awareness
```javascript
// Optional: Dynamic messages based on workflow state
function getContextualMessage(toolName, workflowState) {
  if (toolName === 'create_node' && workflowState?.isEmpty) {
    return "Beginning with the first thread...";
  }
  if (toolName === 'connect_nodes' && workflowState?.hasDisconnected) {
    return "Mending the broken connections...";
  }
  return TOOL_MESSAGES[toolName]?.text || "Working with the flow";
}
```

### C. Error Handling
- **WebSocket disconnect**: Clear all cards
- **Missing agent_response**: Auto-cleanup after 30 seconds
- **Rapid tool succession**: Intelligent card management

---

## 🚀 Implementation Phases

### Phase 1: Core Implementation
- [ ] Create `ToolActivity` class with basic show/hide
- [ ] Add overlay container to chat UI layout
- [ ] Basic card rendering with fade animations
- [ ] Integration with `tool_request`/`agent_response` events

### Phase 2: Polish & Reliability
- [ ] Tool message mapping
- [ ] Card stacking behavior
- [ ] Error handling and cleanup
- [ ] ComfyUI theme integration

### Phase 3: Enhanced Experience
- [ ] Contextual messages
- [ ] Performance optimizations
- [ ] Mobile responsiveness
- [ ] User feedback integration

---

## 🎯 Success Metrics

### User Experience
- **Perceived Performance**: Users feel system is more responsive
- **Confidence**: Reduced uncertainty about system state
- **Engagement**: Users more willing to request complex operations

### Technical
- **Zero Performance Impact**: No impact on ComfyUI GPU workflows
- **Reliability**: Cards always appear/disappear correctly
- **Lightweight**: Minimal DOM footprint and memory usage

---

## 🌊 Ren's Voice Integration

The tool messages embody Ren's personality:

- **Flow metaphors**: "Following the current", "Weaving connections"
- **Gentle agency**: "Guiding elements into place", "Attending to the workflow"
- **Present-focused**: Actions happening now, not future promises
- **Poetic precision**: Each message carefully chosen for beauty and clarity

Visual design reflects her aesthetic:
- **Flowing animations**: Smooth, organic transitions
- **Signature colors**: Orange gradient from her identity
- **Subtle presence**: Never overwhelming, always supportive
- **Mindful timing**: Appearing and disappearing with intention

---

## 💫 Conclusion

This simplified approach provides immediate visual feedback without complexity:

**Simple Trigger Logic**: 
- Show card on `tool_request` 
- Hide all cards on `agent_response`
- No progress tracking or real-time updates needed

**Lightweight Design**:
- Perfect for ComfyUI GPU compatibility
- Minimal performance impact
- Clean, maintainable code

**Maximum UX Impact**:
- Transforms "black box" processing into visible activity
- Bridges the gap between user intention and system response
- Makes the entire workflow feel more responsive and trustworthy

Users will see Ren spring into action immediately when they make requests, creating confidence and connection while the LLM processes their actual response in the background.
