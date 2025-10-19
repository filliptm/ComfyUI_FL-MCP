# Tool Activity Implementation Guide

## 🎯 Implementation Overview

Based on the investigation findings, we'll implement a lightweight tool activity visualization system that shows floating cards above the chat input when tools are executing. The system leverages existing WebSocket events without requiring backend changes.

---

## 📁 File Structure

```
web/js/
├── tool_activity.js          # New: ToolActivity class
├── chat_ui.js               # Modified: Integration + styles
├── extension.js             # Modified: Event handlers
└── ws_client.js             # No changes needed
```

---

## 🔧 Implementation Steps

### Step 1: Create ToolActivity Class

**File**: `web/js/tool_activity.js`

```javascript
/**
 * Tool Activity Visualization for FL_JS
 * Shows floating cards when tools are executing
 */
export class ToolActivity {
    constructor(chatContainer) {
        this.chatContainer = chatContainer;
        this.activeCards = new Map(); // request_id -> {element, timeout}
        this.overlay = null;
        this.maxVisible = 3;
        this.autoCleanupMs = 30000; // 30 seconds fallback
        
        this.toolMessages = {
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
        
        this._createOverlay();
        console.log('[ToolActivity] Initialized');
    }

    /**
     * Show tool activity card
     * @param {string} toolName - Name of the tool being executed
     * @param {string} requestId - Unique request identifier
     */
    showTool(toolName, requestId = 'default') {
        console.log(`[ToolActivity] Showing tool: ${toolName} (${requestId})`);
        
        // Get tool configuration
        const config = this.toolMessages[toolName] || this.toolMessages['*'];
        
        // Create card element
        const card = this._createCard(config, requestId, toolName);
        
        // Add to active cards
        this._addCard(card, requestId);
        
        // Set fallback cleanup timer
        const timeout = setTimeout(() => {
            console.log(`[ToolActivity] Auto-cleanup for ${requestId}`);
            this.hideTool(requestId);
        }, this.autoCleanupMs);
        
        this.activeCards.get(requestId).timeout = timeout;
    }

    /**
     * Hide specific tool card
     * @param {string} requestId - Request identifier to hide
     */
    hideTool(requestId) {
        const cardData = this.activeCards.get(requestId);
        if (!cardData) return;
        
        console.log(`[ToolActivity] Hiding tool: ${requestId}`);
        
        // Clear timeout
        if (cardData.timeout) {
            clearTimeout(cardData.timeout);
        }
        
        // Animate out and remove
        this._removeCard(requestId);
    }

    /**
     * Hide all active tool cards
     */
    hideAllTools() {
        console.log('[ToolActivity] Hiding all tools');
        
        const requestIds = Array.from(this.activeCards.keys());
        requestIds.forEach(requestId => {
            this.hideTool(requestId);
        });
    }

    /**
     * Create overlay container
     * @private
     */
    _createOverlay() {
        this.overlay = document.createElement('div');
        this.overlay.className = 'fl-tool-activity-overlay';
        
        // Insert above input container
        const inputContainer = this.chatContainer.querySelector('.fl-chat-input-container');
        if (inputContainer) {
            inputContainer.parentNode.insertBefore(this.overlay, inputContainer);
        } else {
            // Fallback: append to container
            this.chatContainer.appendChild(this.overlay);
        }
    }

    /**
     * Create tool card element
     * @private
     */
    _createCard(config, requestId, toolName) {
        const card = document.createElement('div');
        card.className = 'fl-tool-card';
        card.dataset.requestId = requestId;
        card.dataset.toolName = toolName;
        
        card.innerHTML = `
            <div class="fl-tool-header">
                <span class="fl-tool-icon">${config.icon}</span>
                <span class="fl-tool-label">Ren is...</span>
            </div>
            <div class="fl-tool-text">${config.text}</div>
            <div class="fl-tool-activity">
                <div class="fl-activity-dot"></div>
                <div class="fl-activity-dot"></div>
                <div class="fl-activity-dot"></div>
            </div>
        `;
        
        return card;
    }

    /**
     * Add card to overlay with animation
     * @private
     */
    _addCard(card, requestId) {
        // Manage card limit
        this._enforceCardLimit();
        
        // Add to DOM
        this.overlay.appendChild(card);
        
        // Store reference
        this.activeCards.set(requestId, { element: card, timeout: null });
        
        // Trigger animation
        requestAnimationFrame(() => {
            card.style.opacity = '1';
            card.style.transform = 'translateY(0)';
        });
    }

    /**
     * Remove card with animation
     * @private
     */
    _removeCard(requestId) {
        const cardData = this.activeCards.get(requestId);
        if (!cardData) return;
        
        const card = cardData.element;
        
        // Animate out
        card.classList.add('exiting');
        
        // Remove after animation
        setTimeout(() => {
            if (card.parentNode) {
                card.parentNode.removeChild(card);
            }
            this.activeCards.delete(requestId);
        }, 300); // Match CSS animation duration
    }

    /**
     * Enforce maximum visible cards
     * @private
     */
    _enforceCardLimit() {
        if (this.activeCards.size >= this.maxVisible) {
            // Remove oldest card
            const oldestRequestId = this.activeCards.keys().next().value;
            this.hideTool(oldestRequestId);
        }
    }

    /**
     * Cleanup all cards (for disconnect/error scenarios)
     */
    cleanup() {
        console.log('[ToolActivity] Cleaning up all cards');
        this.hideAllTools();
    }

    /**
     * Get current activity status
     */
    getStatus() {
        return {
            activeCount: this.activeCards.size,
            activeTools: Array.from(this.activeCards.keys())
        };
    }
}
```

---

### Step 2: Modify ChatUI Integration

**File**: `web/js/chat_ui.js`

#### 2a. Add Import and Initialization

```javascript
// Add to imports at top of file
import { ToolActivity } from './tool_activity.js';

// In constructor, after existing initialization
constructor(container, sessionManager, wsClient, toolExecutor, diagramGenerator) {
    // ... existing code ...
    
    // Initialize tool activity
    this.toolActivity = new ToolActivity(this.container);
    
    // ... rest of existing code ...
}
```

#### 2b. Add CSS Styles to _injectStyles Method

Find the `_injectStyles()` method and add this CSS to the existing template literal:

```javascript
_injectStyles() {
    const style = document.createElement('style');
    style.textContent = `
        /* ... existing styles ... */
        
        /* Tool Activity Overlay */
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
            border: 1px solid rgba(255, 107, 53, 0.3);
            border-radius: 8px;
            padding: 8px 12px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
            backdrop-filter: blur(4px);
            opacity: 0;
            transform: translateY(10px);
            transition: all 0.2s ease-out;
            max-width: 280px;
            font-size: 11px;
            pointer-events: none;
        }

        .fl-tool-card.exiting {
            opacity: 0 !important;
            transform: translateY(10px) !important;
            transition: all 0.3s ease-in !important;
        }

        .fl-tool-header {
            display: flex;
            align-items: center;
            gap: 6px;
            font-weight: 600;
            font-size: 10px;
            color: #ff6b35;
            margin-bottom: 2px;
        }

        .fl-tool-icon {
            font-size: 12px;
        }

        .fl-tool-label {
            opacity: 0.8;
        }

        .fl-tool-text {
            color: #e0e0e0;
            line-height: 1.2;
            opacity: 0.9;
            margin-bottom: 4px;
        }

        .fl-tool-activity {
            display: flex;
            align-items: center;
            gap: 2px;
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
    `;
    
    document.head.appendChild(style);
}
```

#### 2c. Make ChatUI Globally Available

In the constructor, add after all initialization:

```javascript
// Make ChatUI instance globally available for tool activity
if (window.FL_JS) {
    window.FL_JS.chatUI = this;
} else {
    // Ensure FL_JS object exists
    window.FL_JS = { chatUI: this };
}
```

---

### Step 3: Modify Extension Event Handlers

**File**: `web/js/extension.js`

#### 3a. Modify tool_request Handler

Find the existing `tool_request` event handler and modify it:

```javascript
wsClient.on('tool_request', async (message) => {
    console.log("[FL_JS] ⚡ TOOL REQUEST EVENT FIRED:", message.tool_name, 'request_id:', message.request_id);
    
    // Show tool activity card
    try {
        window.FL_JS?.chatUI?.toolActivity?.showTool(
            message.tool_name, 
            message.request_id || 'default'
        );
    } catch (error) {
        console.warn('[FL_JS] Could not show tool activity:', error);
    }
    
    console.log("[FL_JS] ⚡ Calling toolExecutor.executeToolRequest...");
    try {
        await toolExecutor.executeToolRequest(message);
        console.log("[FL_JS] ⚡ toolExecutor.executeToolRequest completed");
    } catch (error) {
        console.error("[FL_JS] ❌ Error in tool execution:", error);
    }
});
```

#### 3b. Modify agent_response Handler

Find the existing `agent_response` event handler and modify it:

```javascript
wsClient.on('agent_response', (message) => {
    console.log("[FL_JS] Agent response received:", message.content);
    
    // Hide all tool activity cards
    try {
        window.FL_JS?.chatUI?.toolActivity?.hideAllTools();
    } catch (error) {
        console.warn('[FL_JS] Could not hide tool activity:', error);
    }
});
```

#### 3c. Add Cleanup on Disconnect

Find disconnect handlers and add cleanup:

```javascript
wsClient.on('disconnected', () => {
    console.log('[FL_JS] WebSocket disconnected');
    
    // Cleanup tool activity
    try {
        window.FL_JS?.chatUI?.toolActivity?.cleanup();
    } catch (error) {
        console.warn('[FL_JS] Could not cleanup tool activity:', error);
    }
});

wsClient.on('error', (error) => {
    console.error("[FL_JS] Error:", error);
    
    // Cleanup tool activity on error
    try {
        window.FL_JS?.chatUI?.toolActivity?.cleanup();
    } catch (error) {
        console.warn('[FL_JS] Could not cleanup tool activity:', error);
    }
});
```

---

## 🧪 Testing Strategy

### Manual Testing

1. **Basic Functionality**:
   ```
   1. Open ComfyUI with FL_JS extension
   2. Send a message that triggers tools (e.g., "show me the workflow")
   3. Verify card appears immediately when tool executes
   4. Verify card disappears when agent response arrives
   ```

2. **Multiple Tools**:
   ```
   1. Send message that triggers multiple tools
   2. Verify multiple cards stack properly
   3. Verify all cards clear on agent response
   ```

3. **Edge Cases**:
   ```
   1. Disconnect WebSocket during tool execution
   2. Send rapid tool requests
   3. Test with different tool types
   ```

### Console Debugging

```javascript
// Add to browser console for debugging
window.FL_JS.chatUI.toolActivity.getStatus()

// Force show test card
window.FL_JS.chatUI.toolActivity.showTool('create_node', 'test-123')

// Force hide all
window.FL_JS.chatUI.toolActivity.hideAllTools()
```

---

## ⚠️ Error Handling

### Graceful Degradation
- Tool activity is purely cosmetic - failures shouldn't break core functionality
- All tool activity calls wrapped in try-catch blocks
- Fallback cleanup timers prevent stuck cards
- Console warnings for debugging but no user-facing errors

### WebSocket Edge Cases
- **Missing agent_response**: Auto-cleanup after 30 seconds
- **Connection drops**: Immediate cleanup of all cards
- **Malformed messages**: Skip tool activity, log warning
- **Missing request_id**: Use 'default' as fallback

---

## 🚀 Deployment Checklist

### Pre-deployment
- [ ] Create `web/js/tool_activity.js`
- [ ] Modify `web/js/chat_ui.js` (import, init, styles, global)
- [ ] Modify `web/js/extension.js` (event handlers, cleanup)
- [ ] Test basic functionality
- [ ] Test error scenarios
- [ ] Verify no performance impact

### Post-deployment
- [ ] Monitor console for errors
- [ ] Verify cards appear/disappear correctly
- [ ] Check compatibility with existing workflows
- [ ] Gather user feedback
- [ ] Performance monitoring during GPU workflows

---

## 🔄 Future Enhancements

### Phase 2 Possibilities
- **Contextual Messages**: Dynamic text based on workflow state
- **Tool Categories**: Group similar tools with shared styling
- **Animation Refinements**: More sophisticated transitions
- **User Preferences**: Toggle tool activity on/off
- **Performance Metrics**: Track tool execution times

### Advanced Features
- **Smart Grouping**: Combine rapid sequential tools
- **Tool History**: Recent tool activity log
- **Custom Messages**: User-defined tool descriptions
- **Workflow Integration**: Show tool impact on canvas

---

## 📊 Success Metrics

### Technical Metrics
- **Zero errors** in console during normal operation
- **<1ms overhead** per tool request
- **100% cleanup** on all disconnect scenarios
- **Compatible** with all existing FL_JS tools

### User Experience Metrics
- **Immediate feedback** when tools execute
- **Clear indication** of system activity
- **No interference** with ComfyUI workflows
- **Consistent** with Ren's personality and aesthetic

---

## 🔧 Implementation Notes

### Key Design Decisions
1. **Event-driven architecture**: Uses existing WebSocket events, no polling
2. **Overlay positioning**: Absolute positioning above input, no layout disruption
3. **Graceful degradation**: Tool activity failures don't break core functionality
4. **Memory efficient**: Cards cleaned up immediately, no accumulation
5. **CSS injection**: Maintains existing styling architecture

### Integration Points
- **WebSocket events**: `tool_request` (show) and `agent_response` (hide)
- **Global access**: Via `window.FL_JS.chatUI.toolActivity`
- **Error boundaries**: Try-catch around all tool activity calls
- **Cleanup hooks**: Disconnect, error, and timeout scenarios

This implementation provides immediate visual feedback while maintaining the lightweight, reliable nature essential for ComfyUI integration. Users will see Ren spring into action the moment they make requests, creating confidence and connection throughout the interaction.
