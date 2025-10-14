# Frontend UI Implementation Plan

## Chat Sidebar Component - ComfyUI Native Integration

### Integration Method

**Using ComfyUI's Native Sidebar System** via `app.extensionManager.registerSidebarTab()`

- ✅ Integrates with existing ComfyUI left panel drawer system
- ✅ Gets its own button in the sidebar
- ✅ Follows ComfyUI's UI patterns and styling
- ✅ No layout conflicts or z-index issues
- ✅ Users already familiar with this interface pattern

**Reference:** `legacy/NodePackLoader_SideBar.js` shows the pattern

### Sidebar Registration

```javascript
// frontend/extension.js (ComfyUI extension entry point)

import { app } from '../../scripts/app.js';
import { SessionManager } from './session_manager.js';
import { WSClient } from './ws_client.js';
import { ToolExecutor } from './tool_executor.js';
import { QueryExecutor } from './query_executor.js';
import { DiagramGenerator } from './diagram_generator.js';
import { FLAPI } from './fl_api.js';
import { ChatUI } from './chat_ui.js';

let chatUI = null;
let wsClient = null;

app.extensionManager.registerSidebarTab({
    id: 'FL_JS_Assistant',
    icon: 'pi pi-comments', // Chat/assistant icon
    title: 'FL_JS Assistant',
    tooltip: 'AI assistant for workflow creation and modification',
    type: 'custom',
    render: (el) => {
        // Initialize on first render
        if (!chatUI) {
            // Initialize session
            const sessionManager = new SessionManager();
            const sessionId = sessionManager.sessionId;
            
            // Initialize FL_JS API wrapper
            const flApi = new FLAPI(app);
            
            // Initialize query and diagram tools
            const queryExecutor = new QueryExecutor(app.graph);
            const diagramGenerator = new DiagramGenerator(app.graph);
            
            // Initialize tool executor
            const toolExecutor = new ToolExecutor(flApi, queryExecutor, diagramGenerator);
            
            // Initialize WebSocket client
            wsClient = new WSClient(sessionId, 'ws://localhost:8000/ws');
            
            // Connect tool executor to WebSocket
            wsClient.on('tool_request', async (data) => {
                const result = await toolExecutor.execute(data);
                wsClient.sendToolResult(data.request_id, result);
            });
            
            // Initialize Chat UI
            chatUI = new ChatUI(el, wsClient);
            
            // Connect WebSocket
            wsClient.connect();
            
            console.log('[FL_JS] Chat assistant initialized');
        } else {
            // Re-render existing UI into new element
            chatUI.rerender(el);
        }
    },
});
```

### Chat UI Component

```javascript
// frontend/chat_ui.js

class ChatUI {
    constructor(containerEl, wsClient) {
        this.container = containerEl;
        this.wsClient = wsClient;
        this.messages = [];
        
        this.render();
        this.setupWebSocketHandlers();
    }
    
    render() {
        // Style the container
        this.container.style.backgroundColor = '#18181b';
        this.container.style.color = '#cccccc';
        this.container.style.height = '100%';
        this.container.style.display = 'flex';
        this.container.style.flexDirection = 'column';
        this.container.style.padding = '0';
        this.container.style.overflow = 'hidden';
        
        // Create UI structure
        this.createHeader();
        this.createMessagesContainer();
        this.createTypingIndicator();
        this.createInputArea();
        
        // Load Mermaid
        this.loadMermaid();
    }
    
    rerender(newContainer) {
        // Called when sidebar is reopened
        this.container = newContainer;
        this.render();
        // Re-add all messages
        this.messages.forEach(msg => {
            this.appendMessageToDOM(msg.type, msg.content);
        });
    }
    
    createHeader() {
        const header = document.createElement('div');
        header.style.padding = '12px 16px';
        header.style.backgroundColor = '#252525';
        header.style.borderBottom = '1px solid #333';
        header.style.display = 'flex';
        header.style.justifyContent = 'space-between';
        header.style.alignItems = 'center';
        header.style.flexShrink = '0';
        
        const title = document.createElement('h3');
        title.textContent = 'FL_JS Assistant';
        title.style.margin = '0';
        title.style.fontSize = '14px';
        title.style.fontWeight = '600';
        title.style.color = '#fff';
        header.appendChild(title);
        
        // Connection status
        const statusContainer = document.createElement('div');
        statusContainer.style.display = 'flex';
        statusContainer.style.alignItems = 'center';
        statusContainer.style.gap = '6px';
        statusContainer.style.fontSize = '11px';
        statusContainer.style.color = '#999';
        
        this.statusDot = document.createElement('span');
        this.statusDot.style.width = '8px';
        this.statusDot.style.height = '8px';
        this.statusDot.style.borderRadius = '50%';
        this.statusDot.style.backgroundColor = '#666';
        statusContainer.appendChild(this.statusDot);
        
        this.statusText = document.createElement('span');
        this.statusText.textContent = 'Connecting...';
        statusContainer.appendChild(this.statusText);
        
        header.appendChild(statusContainer);
        this.container.appendChild(header);
    }
    
    createMessagesContainer() {
        this.messagesContainer = document.createElement('div');
        this.messagesContainer.style.flex = '1';
        this.messagesContainer.style.overflowY = 'auto';
        this.messagesContainer.style.padding = '16px';
        this.messagesContainer.style.display = 'flex';
        this.messagesContainer.style.flexDirection = 'column';
        this.messagesContainer.style.gap = '12px';
        
        // Scrollbar styling
        this.messagesContainer.style.scrollbarWidth = 'thin';
        this.messagesContainer.style.scrollbarColor = '#444 #1e1e1e';
        
        this.container.appendChild(this.messagesContainer);
    }
    
    createTypingIndicator() {
        this.typingIndicator = document.createElement('div');
        this.typingIndicator.style.display = 'none';
        this.typingIndicator.style.padding = '8px 16px';
        this.typingIndicator.style.alignItems = 'center';
        this.typingIndicator.style.gap = '4px';
        this.typingIndicator.style.flexShrink = '0';
        
        for (let i = 0; i < 3; i++) {
            const dot = document.createElement('span');
            dot.style.width = '6px';
            dot.style.height = '6px';
            dot.style.borderRadius = '50%';
            dot.style.backgroundColor = '#666';
            dot.style.display = 'inline-block';
            dot.style.animation = `typing 1.4s infinite ${i * 0.2}s`;
            this.typingIndicator.appendChild(dot);
        }
        
        // Add animation keyframes
        if (!document.getElementById('fl-typing-animation')) {
            const style = document.createElement('style');
            style.id = 'fl-typing-animation';
            style.textContent = `
                @keyframes typing {
                    0%, 60%, 100% { transform: translateY(0); }
                    30% { transform: translateY(-8px); }
                }
            `;
            document.head.appendChild(style);
        }
        
        this.container.appendChild(this.typingIndicator);
    }
    
    createInputArea() {
        const inputContainer = document.createElement('div');
        inputContainer.style.padding = '12px 16px';
        inputContainer.style.backgroundColor = '#252525';
        inputContainer.style.borderTop = '1px solid #333';
        inputContainer.style.display = 'flex';
        inputContainer.style.gap = '8px';
        inputContainer.style.alignItems = 'flex-end';
        inputContainer.style.flexShrink = '0';
        
        this.inputField = document.createElement('textarea');
        this.inputField.placeholder = 'Ask me about your workflow...';
        this.inputField.rows = 1;
        this.inputField.style.flex = '1';
        this.inputField.style.backgroundColor = '#1e1e1e';
        this.inputField.style.border = '1px solid #444';
        this.inputField.style.borderRadius = '6px';
        this.inputField.style.padding = '8px 10px';
        this.inputField.style.color = '#fff';
        this.inputField.style.fontFamily = 'inherit';
        this.inputField.style.fontSize = '13px';
        this.inputField.style.resize = 'none';
        this.inputField.style.maxHeight = '100px';
        this.inputField.style.overflowY = 'auto';
        
        // Auto-resize on input
        this.inputField.addEventListener('input', () => {
            this.inputField.style.height = 'auto';
            this.inputField.style.height = this.inputField.scrollHeight + 'px';
        });
        
        // Enter to send (Shift+Enter for new line)
        this.inputField.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });
        
        inputContainer.appendChild(this.inputField);
        
        const sendButton = document.createElement('button');
        sendButton.textContent = '➤';
        sendButton.style.backgroundColor = '#2196f3';
        sendButton.style.border = 'none';
        sendButton.style.borderRadius = '6px';
        sendButton.style.width = '36px';
        sendButton.style.height = '36px';
        sendButton.style.color = '#fff';
        sendButton.style.fontSize = '16px';
        sendButton.style.cursor = 'pointer';
        sendButton.style.flexShrink = '0';
        sendButton.onclick = () => this.sendMessage();
        
        sendButton.addEventListener('mouseenter', () => {
            sendButton.style.backgroundColor = '#1976d2';
        });
        sendButton.addEventListener('mouseleave', () => {
            sendButton.style.backgroundColor = '#2196f3';
        });
        
        inputContainer.appendChild(sendButton);
        this.container.appendChild(inputContainer);
    }
    
    loadMermaid() {
        // Load Mermaid.js for diagram rendering
        if (!window.mermaid) {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js';
            script.onload = () => {
                mermaid.initialize({ 
                    startOnLoad: true,
                    theme: 'dark',
                    securityLevel: 'loose'
                });
            };
            document.head.appendChild(script);
        }
    }
    
    setupWebSocketHandlers() {
        this.wsClient.on('agent_response', (data) => {
            this.handleAgentResponse(data);
        });
        
        this.wsClient.on('typing_indicator', (data) => {
            this.handleTypingIndicator(data);
        });
        
        this.wsClient.on('error', (data) => {
            this.handleError(data);
        });
        
        this.wsClient.on('connection_status', (status) => {
            this.updateConnectionStatus(status);
        });
    }
    
    sendMessage() {
        const content = this.inputField.value.trim();
        if (!content) return;
        
        // Add user message to UI
        this.addMessage('user', content);
        
        // Send via WebSocket
        this.wsClient.sendUserMessage(content);
        
        // Clear input
        this.inputField.value = '';
        this.inputField.style.height = 'auto';
    }
    
    addMessage(type, content) {
        // Store message
        this.messages.push({ type, content, timestamp: new Date() });
        
        // Add to DOM
        this.appendMessageToDOM(type, content);
        
        // Scroll to bottom
        this.scrollToBottom();
    }
    
    appendMessageToDOM(type, content) {
        const messageEl = document.createElement('div');
        messageEl.style.display = 'flex';
        messageEl.style.flexDirection = 'column';
        messageEl.style.gap = '4px';
        messageEl.style.maxWidth = type === 'user' ? '85%' : '100%';
        messageEl.style.alignSelf = type === 'user' ? 'flex-end' : 'flex-start';
        
        if (type === 'agent') {
            messageEl.style.flexDirection = 'row';
            messageEl.style.gap = '8px';
            
            // Avatar
            const avatar = document.createElement('div');
            avatar.textContent = '🤖';
            avatar.style.width = '28px';
            avatar.style.height = '28px';
            avatar.style.borderRadius = '50%';
            avatar.style.backgroundColor = '#333';
            avatar.style.display = 'flex';
            avatar.style.alignItems = 'center';
            avatar.style.justifyContent = 'center';
            avatar.style.flexShrink = '0';
            avatar.style.fontSize = '14px';
            messageEl.appendChild(avatar);
        }
        
        const contentEl = document.createElement('div');
        contentEl.style.padding = '8px 12px';
        contentEl.style.borderRadius = type === 'user' ? '10px 10px 0 10px' : '10px 10px 10px 0';
        contentEl.style.wordWrap = 'break-word';
        contentEl.style.fontSize = '13px';
        contentEl.style.lineHeight = '1.5';
        
        switch (type) {
            case 'user':
                contentEl.style.backgroundColor = '#2196f3';
                contentEl.style.color = '#fff';
                contentEl.textContent = content;
                break;
            case 'agent':
                contentEl.style.backgroundColor = '#2a2a2a';
                contentEl.style.color = '#e0e0e0';
                contentEl.style.flex = '1';
                contentEl.innerHTML = this.renderMarkdown(content);
                break;
            case 'error':
                contentEl.style.backgroundColor = '#3d1f1f';
                contentEl.style.color = '#ff6b6b';
                contentEl.style.borderLeft = '3px solid #f44336';
                contentEl.innerHTML = `<strong>Error:</strong> ${this.escapeHtml(content)}`;
                break;
            case 'system':
                contentEl.style.backgroundColor = 'transparent';
                contentEl.style.color = '#999';
                contentEl.style.fontSize = '11px';
                contentEl.style.textAlign = 'center';
                contentEl.style.fontStyle = 'italic';
                contentEl.textContent = content;
                messageEl.style.alignSelf = 'center';
                break;
        }
        
        messageEl.appendChild(contentEl);
        this.messagesContainer.appendChild(messageEl);
        
        // Render Mermaid diagrams
        if (type === 'agent' && content.includes('```mermaid')) {
            setTimeout(() => {
                if (window.mermaid) {
                    mermaid.run({
                        querySelector: '.mermaid:not([data-processed])'
                    });
                }
            }, 100);
        }
    }
    
    renderMarkdown(content) {
        let html = content;
        
        // Code blocks (including mermaid)
        html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, (match, lang, code) => {
            if (lang === 'mermaid') {
                return `<div class="mermaid" style="background: #fff; padding: 12px; border-radius: 6px; margin: 8px 0;">${code}</div>`;
            }
            return `<pre style="background: #1a1a1a; padding: 10px; border-radius: 4px; overflow-x: auto; margin: 6px 0;"><code>${this.escapeHtml(code)}</code></pre>`;
        });
        
        // Inline code
        html = html.replace(/`([^`]+)`/g, '<code style="background: #1a1a1a; padding: 2px 5px; border-radius: 3px; font-family: monospace; font-size: 12px;">$1</code>');
        
        // Bold
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        
        // Italic
        html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        
        // Line breaks
        html = html.replace(/\n/g, '<br>');
        
        return html;
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    handleAgentResponse(data) {
        const { content, is_final } = data;
        
        if (is_final) {
            this.addMessage('agent', content);
        }
    }
    
    handleTypingIndicator(data) {
        if (data.is_typing) {
            this.typingIndicator.style.display = 'flex';
            this.scrollToBottom();
        } else {
            this.typingIndicator.style.display = 'none';
        }
    }
    
    handleError(data) {
        this.addMessage('error', data.message);
    }
    
    updateConnectionStatus(status) {
        const statusConfig = {
            'connected': { color: '#4caf50', text: 'Connected', animation: 'pulse 2s infinite' },
            'connecting': { color: '#ff9800', text: 'Connecting...', animation: 'pulse 1s infinite' },
            'disconnected': { color: '#f44336', text: 'Disconnected', animation: 'none' },
            'reconnected': { color: '#4caf50', text: 'Connected', animation: 'pulse 2s infinite' }
        };
        
        const config = statusConfig[status] || statusConfig['connecting'];
        
        this.statusDot.style.backgroundColor = config.color;
        this.statusDot.style.animation = config.animation;
        this.statusText.textContent = config.text;
        
        // Add animation keyframes if not exists
        if (!document.getElementById('fl-pulse-animation')) {
            const style = document.createElement('style');
            style.id = 'fl-pulse-animation';
            style.textContent = `
                @keyframes pulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.5; }
                }
            `;
            document.head.appendChild(style);
        }
        
        if (status === 'disconnected') {
            this.addMessage('system', 'Connection lost. Attempting to reconnect...');
        } else if (status === 'reconnected') {
            this.addMessage('system', 'Reconnected successfully.');
        }
    }
    
    scrollToBottom() {
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }
    
    clear() {
        this.messagesContainer.innerHTML = '';
        this.messages = [];
    }
}

export { ChatUI };
```

## Key Differences from Original Plan

### ✅ What Changed:

1. **Integration Method**
   - **Old:** Fixed `position: fixed` overlay on right side
   - **New:** Native ComfyUI sidebar tab via `app.extensionManager.registerSidebarTab()`

2. **UI Location**
   - **Old:** Always visible right sidebar (400px wide)
   - **New:** Left panel drawer with button (ComfyUI standard)

3. **Styling**
   - **Old:** Separate CSS file
   - **New:** Inline styles matching ComfyUI's dark theme (#18181b, #252525, etc.)

4. **User Experience**
   - **Old:** Minimize button + floating toggle
   - **New:** Standard sidebar toggle (users already know how to use it)

### ✅ What Stayed the Same:

- All message types (user, agent, error, system)
- Markdown rendering with Mermaid support
- WebSocket integration
- Typing indicators
- Connection status display
- Auto-resize textarea
- Message history
- All functionality

## Visual Layout

```
┌─┬───────────────────────────────────────────────────┐
│☰│                                                   │
│📦│          ComfyUI Canvas                          │
│💬│          (Workflow Graph)                        │ ← FL_JS button here
│⚙│                                                   │
│ │                                                   │
│ │                                                   │
└─┴───────────────────────────────────────────────────┘

When FL_JS button clicked:

┌────────────────┬──────────────────────────────────────┐
│  FL_JS Chat    │                                      │
│  ┌──────────┐  │     ComfyUI Canvas                   │
│  │ Header   │  │     (Workflow Graph)                 │
│  ├──────────┤  │                                      │
│  │          │  │                                      │
│  │ Messages │  │                                      │
│  │          │  │                                      │
│  ├──────────┤  │                                      │
│  │ Input    │  │                                      │
│  └──────────┘  │                                      │
└────────────────┴──────────────────────────────────────┘
```

## Implementation Files

### Files to Create:
- `frontend/extension.js` - Extension entry point with sidebar registration
- `frontend/chat_ui.js` - Chat UI component (updated for sidebar)
- `frontend/ws_client.js` - WebSocket client
- `frontend/session_manager.js` - Session management
- `frontend/tool_executor.js` - Tool execution handler
- `frontend/query_executor.js` - Query DSL executor
- `frontend/fl_api.js` - FL_JS API wrapper
- `frontend/diagram_generator.js` - Mermaid diagram generation

### No Longer Needed:
- ~~`frontend/styles.css`~~ - Using inline styles instead

## Summary

✅ **Native ComfyUI integration** via sidebar tab system
✅ **Familiar UX** - users already know how to use left panel
✅ **No layout conflicts** - follows ComfyUI patterns
✅ **Consistent styling** - matches ComfyUI dark theme
✅ **All functionality preserved** - just better integrated
✅ **Reference implementation** - NodePackLoader_SideBar.js as guide
