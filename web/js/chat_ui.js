/**
 * Chat UI - Native ComfyUI sidebar chat interface
 * 
 * This module provides a chat interface that integrates with ComfyUI's native
 * sidebar system. It handles message display, user input, markdown rendering,
 * and Mermaid diagram rendering.
 * 
 * @module chat_ui
 */

import { marked } from "https://cdn.jsdelivr.net/npm/marked@11.1.1/+esm";
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.6.1/+esm";
import { ToolActivity } from './tool_activity.js';

/**
 * ChatUI class - Manages chat interface and message rendering
 */
export class ChatUI {
    constructor(container, wsClient) {
        this.container = container;
        this.wsClient = wsClient;
        this.messages = [];
        this.isTyping = false;
        
        // Create a custom renderer
        const renderer = new marked.Renderer();

        // Override the default link renderer
        renderer.link = function(href, title, text) {
        // Escape quotes in title if necessary
        const safeTitle = title ? ` title="${title}"` : "";
        return `<a href="${href}"${safeTitle} target="_blank" rel="noopener noreferrer">${text}</a>`;
        };

        // Configure marked for safe rendering
        marked.setOptions({
            breaks: true,
            gfm: true,
            headerIds: false,
            mangle: false,
            renderer
        });
        
        // Configure mermaid
        mermaid.initialize({
            startOnLoad: false,
            theme: 'dark',
            securityLevel: 'loose',
            fontFamily: 'monospace'
        });
        
        // Initialize UI
        this._initializeUI();
        this._attachEventHandlers();
        
        // Initialize tool activity after UI is ready
        this.toolActivity = new ToolActivity(this.container);
        
        // Make ChatUI globally available for tool activity access
        if (window.FL_JS) {
            window.FL_JS.chatUI = this;
        } else {
            window.FL_JS = { chatUI: this };
        }
        
        console.log('[ChatUI] Initialized with tool activity');
    }

    /**
     * Initialize UI structure
     * @private
     */
    _initializeUI() {
        // Clear container
        this.container.innerHTML = '';

        // Ensure the parent container doesn't scroll
        this.container.style.overflow = 'hidden';
        this.container.style.height = '100%';
        this.container.style.display = 'flex';
        this.container.style.flexDirection = 'column';

        // Create main layout
        const layout = document.createElement('div');
        layout.className = 'fl-chat-layout';
        layout.innerHTML = `
            <div class="fl-chat-header">
                <div class="fl-chat-title">Ren</div>
                <div class="fl-chat-status">
                    <span class="fl-status-indicator" id="fl-status-indicator"></span>
                    <span class="fl-status-text" id="fl-status-text">Connecting...</span>
                </div>
            </div>
            <div class="fl-chat-messages" id="fl-chat-messages"></div>
            <div class="fl-chat-typing" id="fl-chat-typing" style="display: none;">
                <span class="fl-typing-indicator">
                    <span></span><span></span><span></span>
                </span>
                <span class="fl-typing-text">Assistant is working...</span>
            </div>
            <div class="fl-chat-input-container">
                <textarea
                    class="fl-chat-input"
                    id="fl-chat-input"
                    placeholder="Ask me anything about your workflow..."
                    rows="1"
                ></textarea>
                <button class="fl-chat-send" id="fl-chat-send" title="Send message (Enter)">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="22" y1="2" x2="11" y2="13"></line>
                        <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                    </svg>
                </button>
            </div>
        `;

        this.container.appendChild(layout);
        
        // Store references using querySelector on container
        this.messagesContainer = this.container.querySelector('#fl-chat-messages');
        this.inputField = this.container.querySelector('#fl-chat-input');
        this.sendButton = this.container.querySelector('#fl-chat-send');
        this.typingIndicator = this.container.querySelector('#fl-chat-typing');
        this.statusIndicator = this.container.querySelector('#fl-status-indicator');
        this.statusText = this.container.querySelector('#fl-status-text');
        
        // Add styles
        this._injectStyles();
        
        // Add debug styles for tool activity
        const debugStyle = document.createElement('style');
        debugStyle.textContent = `
          .fl-chat-layout {
            overflow: visible !important;
            min-height: 100px; /* Ensure space for tool cards */
          }
          .fl-tool-activity-overlay {
            bottom: 20px !important; /* Fixed position above input */
            max-height: 60vh; /* Prevent overflow */
          }
        `;
        document.head.appendChild(debugStyle);
        
        // ✅ FIX #2: Verify DOM is ready before adding welcome message
        if (this.messagesContainer) {
            // Add welcome message after a tick to ensure DOM is fully inserted
            requestAnimationFrame(() => {
                this._addWelcomeMessage();
            });
        } else {
            console.error('[ChatUI] Failed to initialize: messagesContainer not found');
            // Retry after delay
            setTimeout(() => {
                this.messagesContainer = this.container.querySelector('#fl-chat-messages');
                if (this.messagesContainer) {
                    this._addWelcomeMessage();
                } else {
                    console.error('[ChatUI] messagesContainer still not found after retry');
                }
            }, 100);
        }
    }

    /**
     * Inject CSS styles
     * @private
     */
    _injectStyles() {
        if (document.getElementById('fl-chat-styles')) return;
        
        const style = document.createElement('style');
        style.id = 'fl-chat-styles';
        style.textContent = `
            /* Chat Layout */
            .fl-chat-layout {
                display: flex;
                flex-direction: column;
                height: 100%;
                background: var(--bg-color, #1e1e1e);
                color: var(--fg-color, #e0e0e0);
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                overflow: hidden; /* Prevent layout itself from scrolling */
                position: relative; /* For tool activity overlay positioning */
            }

            /* Header */
            .fl-chat-header {
                padding: 12px 16px;
                border-bottom: 1px solid var(--border-color, #333);
                display: flex;
                justify-content: space-between;
                align-items: center;
                flex-shrink: 0; /* Keep header fixed at top */
                background: var(--bg-color, #1e1e1e); /* Ensure header has background */
            }

            .fl-chat-title {
                font-weight: 600;
                font-size: 14px;
            }

            .fl-chat-status {
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 12px;
            }

            .fl-status-indicator {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: #666;
                transition: background 0.3s;
            }

            .fl-status-indicator.connected {
                background: #4caf50;
                box-shadow: 0 0 4px #4caf50;
            }

            .fl-status-indicator.disconnected {
                background: #f44336;
            }

            .fl-status-indicator.connecting {
                background: #ff9800;
                animation: pulse 1.5s infinite;
            }

            @keyframes pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.5; }
            }

            /* Messages Container */
            .fl-chat-messages {
                flex: 1; /* Take up remaining space */
                overflow-y: auto; /* Only messages scroll */
                overflow-x: hidden; /* Prevent horizontal scroll */
                padding: 16px;
                display: flex;
                flex-direction: column;
                gap: 12px;
                min-height: 0; /* Critical for flex scroll to work */
            }

            .fl-chat-messages::-webkit-scrollbar {
                width: 8px;
            }

            .fl-chat-messages::-webkit-scrollbar-track {
                background: transparent;
            }

            .fl-chat-messages::-webkit-scrollbar-thumb {
                background: var(--border-color, #333);
                border-radius: 4px;
            }

            .fl-chat-messages::-webkit-scrollbar-thumb:hover {
                background: #555;
            }

            /* Message Bubble */
            .fl-message {
                display: flex;
                flex-direction: column;
                gap: 4px;
                max-width: 85%;
                animation: slideIn 0.2s ease-out;
            }

            @keyframes slideIn {
                from {
                    opacity: 0;
                    transform: translateY(10px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }

            .fl-message.user {
                align-self: flex-end;
            }

            .fl-message.assistant {
                align-self: flex-start;
            }

            .fl-message.system {
                align-self: center;
                max-width: 95%;
            }

            .fl-message-header {
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 11px;
                opacity: 0.7;
                padding: 0 8px;
            }

            .fl-message-role {
                font-weight: 600;
            }

            .fl-message-time {
                font-size: 10px;
            }

            .fl-message-content {
                padding: 10px 14px;
                border-radius: 12px;
                line-height: 1.5;
                font-size: 13px;
                word-wrap: break-word;
            }

            .fl-message.user .fl-message-content {
                background: var(--comfy-input-bg, #2a2a2a);
                border: 1px solid var(--border-color, #333);
            }

            .fl-message.assistant .fl-message-content {
                background: var(--comfy-menu-bg, #252525);
                border: 1px solid var(--border-color, #333);
            }

            .fl-message.error .fl-message-content {
                background: rgba(244, 67, 54, 0.1);
                border: 1px solid rgba(244, 67, 54, 0.3);
                color: #ff6b6b;
            }

            .fl-message.system .fl-message-content {
                background: rgba(33, 150, 243, 0.1);
                border: 1px solid rgba(33, 150, 243, 0.3);
                color: #64b5f6;
                text-align: center;
                font-size: 12px;
            }

            /* Ren Welcome Message */
            .fl-message.ren-welcome .fl-message-content {
                background: linear-gradient(135deg, #ff6b35 0%, #c44536 100%);
                border: none;
                color: white;
                text-align: left;
                font-size: 13px;
                padding: 16px;
            }

            .fl-message.ren-welcome .fl-message-header {
                display: none;
            }

            /* Starter Questions */
            .fl-starter-questions {
                display: flex;
                flex-direction: column;
                gap: 8px;
                margin-top: 12px;
            }

            .fl-starter-question {
                background: rgba(255, 255, 255, 0.1);
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 8px;
                padding: 10px 12px;
                cursor: pointer;
                transition: all 0.2s;
                color: white;
                font-size: 13px;
                line-height: 1.4;
            }

            .fl-starter-question:hover {
                background: rgba(255, 255, 255, 0.2);
                border-color: rgba(255, 255, 255, 0.3);
                transform: translateX(2px);
            }

            /* Markdown Styles */
            .fl-message-content p {
                margin: 0 0 8px 0;
            }

            .fl-message-content p:last-child {
                margin-bottom: 0;
            }

            .fl-message-content code {
                background: rgba(0, 0, 0, 0.3);
                padding: 2px 6px;
                border-radius: 3px;
                font-family: 'Courier New', monospace;
                font-size: 12px;
            }

            .fl-message-content pre {
                background: rgba(0, 0, 0, 0.3);
                padding: 12px;
                border-radius: 6px;
                overflow-x: auto;
                margin: 8px 0;
            }

            .fl-message-content pre code {
                background: transparent;
                padding: 0;
            }

            .fl-message-content ul, .fl-message-content ol {
                margin: 8px 0;
                padding-left: 24px;
            }

            .fl-message-content li {
                margin: 4px 0;
            }

            .fl-message-content a {
                color: #64b5f6;
                text-decoration: none;
            }

            .fl-message-content a:hover {
                text-decoration: underline;
            }

            .fl-message-content blockquote {
                border-left: 3px solid var(--border-color, #333);
                padding-left: 12px;
                margin: 8px 0;
                opacity: 0.8;
            }

            /* Mermaid Diagram */
            .fl-mermaid-container {
                background: rgba(0, 0, 0, 0.2);
                padding: 16px;
                border-radius: 8px;
                margin: 8px 0;
                overflow-x: auto;
            }

            .fl-mermaid-container svg {
                max-width: 100%;
                height: auto;
            }

            /* Typing Indicator */
            .fl-chat-typing {
                padding: 8px 16px;
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 12px;
                opacity: 0.7;
                flex-shrink: 0; /* Keep typing indicator fixed */
                background: var(--bg-color, #1e1e1e); /* Ensure background */
                border-top: 1px solid var(--border-color, #333);
            }

            .fl-typing-indicator {
                display: flex;
                gap: 4px;
            }

            .fl-typing-indicator span {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: var(--fg-color, #e0e0e0);
                animation: bounce 1.4s infinite ease-in-out;
            }

            .fl-typing-indicator span:nth-child(1) {
                animation-delay: -0.32s;
            }

            .fl-typing-indicator span:nth-child(2) {
                animation-delay: -0.16s;
            }

            @keyframes bounce {
                0%, 80%, 100% {
                    transform: scale(0);
                }
                40% {
                    transform: scale(1);
                }
            }

            /* Input Container */
            .fl-chat-input-container {
                padding: 12px 16px;
                border-top: 1px solid var(--border-color, #333);
                display: flex;
                gap: 8px;
                align-items: flex-end;
                flex-shrink: 0; /* Keep input fixed at bottom */
                background: var(--bg-color, #1e1e1e); /* Ensure background */
                position: relative; /* For tool activity overlay positioning */
            }

            .fl-chat-input {
                flex: 1;
                background: var(--comfy-input-bg, #2a2a2a);
                border: 1px solid var(--border-color, #333);
                border-radius: 8px;
                padding: 10px 12px;
                color: var(--fg-color, #e0e0e0);
                font-size: 13px;
                font-family: inherit;
                resize: none;
                max-height: 120px;
                overflow-y: auto;
                transition: border-color 0.2s;
            }

            .fl-chat-input:focus {
                outline: none;
                border-color: var(--comfy-input-focus, #555);
            }

            .fl-chat-input::placeholder {
                color: var(--fg-color, #666);
                opacity: 0.5;
            }

            .fl-chat-send {
                background: var(--comfy-input-bg, #2a2a2a);
                border: 1px solid var(--border-color, #333);
                border-radius: 8px;
                width: 40px;
                height: 40px;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                color: var(--fg-color, #e0e0e0);
                transition: all 0.2s;
                flex-shrink: 0;
            }

            .fl-chat-send:hover {
                background: var(--comfy-input-focus, #333);
                border-color: var(--comfy-input-focus, #555);
            }

            .fl-chat-send:active {
                transform: scale(0.95);
            }

            .fl-chat-send:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }

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
                animation: toolPulse 1.4s ease-in-out infinite;
            }

            .fl-activity-dot:nth-child(2) {
                animation-delay: 0.2s;
            }

            .fl-activity-dot:nth-child(3) {
                animation-delay: 0.4s;
            }

            @keyframes toolPulse {
                0%, 80%, 100% { opacity: 0.3; }
                40% { opacity: 1; }
            }
        `;
        
        document.head.appendChild(style);
    }

    /**
     * Attach event handlers
     * @private
     */
    _attachEventHandlers() {
        // Send button click
        this.sendButton.addEventListener('click', () => this._sendMessage());
        
        // Enter to send (Shift+Enter for newline)
        this.inputField.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this._sendMessage();
            }
        });
        
        // Auto-resize textarea
        this.inputField.addEventListener('input', () => {
            this.inputField.style.height = 'auto';
            this.inputField.style.height = this.inputField.scrollHeight + 'px';
        });
        
        // WebSocket event handlers
        if (this.wsClient) {
            this.wsClient.on('connected', () => this._updateConnectionStatus('connected'));
            this.wsClient.on('disconnected', () => {
                this._updateConnectionStatus('disconnected');
                // Cleanup tool activity on disconnect
                try {
                    this.toolActivity?.cleanup();
                } catch (error) {
                    console.warn('[ChatUI] Could not cleanup tool activity on disconnect:', error);
                }
            });
            this.wsClient.on('connecting', () => this._updateConnectionStatus('connecting'));
            this.wsClient.on('agent_response', (data) => {
                // Hide all tool activity cards on agent response
                try {
                    this.toolActivity?.hideAllTools();
                } catch (error) {
                    console.warn('[ChatUI] Could not hide tool activity on agent response:', error);
                }
                this._handleAgentResponse(data);
            });
            this.wsClient.on('error', (data) => {
                // Cleanup tool activity on error
                try {
                    this.toolActivity?.cleanup();
                } catch (error) {
                    console.warn('[ChatUI] Could not cleanup tool activity on error:', error);
                }
                this._handleError(data);
            });
        }
    }

    /**
     * Add welcome message with Ren introduction
     * @private
     */
    _addWelcomeMessage() {
        const message = {
            role: 'ren-welcome',
            content: '',
            timestamp: new Date(),
            displayRole: 'ren-welcome'
        };
        
        this.messages.push(message);
        this._renderRenWelcome(message);
        this._scrollToBottom();
    }

    /**
     * Render Ren's welcome message with starter questions
     * @private
     */
    _renderRenWelcome(message) {
        if (!this.messagesContainer) {
            console.error('[ChatUI] Cannot render welcome, messagesContainer not ready');
            return;
        }
        
        const messageEl = document.createElement('div');
        messageEl.className = 'fl-message ren-welcome';
        
        const contentEl = document.createElement('div');
        contentEl.className = 'fl-message-content';
        
        // Ren's introduction
        const intro = document.createElement('div');
        intro.innerHTML = `<strong>I'm Ren (蓮)</strong>, your ComfyUI workflow assistant.<br>Think of me as the bridge between what you imagine and what you create.`;
        contentEl.appendChild(intro);
        
        // Starter questions
        const starterQuestions = [
            "What does this workflow do?",
            "Help me build a text-to-image workflow",
            "What nodes do I have for upscaling?",
            "Explain how the sampler works",
            "My workflow isn't working—can you help debug it?",
            "How can I organize my nodes better?"
        ];
        
        const questionsContainer = document.createElement('div');
        questionsContainer.className = 'fl-starter-questions';
        
        starterQuestions.forEach(question => {
            const questionEl = document.createElement('div');
            questionEl.className = 'fl-starter-question';
            questionEl.textContent = question;
            questionEl.addEventListener('click', () => {
                this.inputField.value = question;
                this.inputField.focus();
                // Auto-resize after setting value
                this.inputField.style.height = 'auto';
                this.inputField.style.height = this.inputField.scrollHeight + 'px';
                // Send it
                (async () => {
                    await this._sendMessage();
                })();
            });
            questionsContainer.appendChild(questionEl);
        });
        
        contentEl.appendChild(questionsContainer);
        messageEl.appendChild(contentEl);
        
        this.messagesContainer.appendChild(messageEl);
    }

    /**
     * Send user message
     * @private
     */
    async _sendMessage() {
        const message = this.inputField.value.trim();
        if (!message) return;
        
        // Add user message to UI
        this.addMessage('user', message);
        
        // Clear input
        this.inputField.value = '';
        this.inputField.style.height = 'auto';
        
        // Show typing indicator
        this.setTyping(true);
        
        // Send to backend
        try {
            await this.wsClient.send({
                type: 'user_message',
                message: message
            });
        } catch (error) {
            console.error('[ChatUI] Error sending message:', error);
            this.addMessage('error', `Failed to send message: ${error.message}`);
            this.setTyping(false);
        }
    }

    /**
     * Handle agent response
     * @private
     */
    _handleAgentResponse(data) {
        this.setTyping(false);
        
        if (data.message) {
            this.addMessage('assistant', data.message);
        }
    }

    /**
     * Handle error
     * @private
     */
    _handleError(data) {
        this.setTyping(false);
        this.addMessage('error', data.error || 'An error occurred');
    }

    /**
     * Add message to chat
     * @param {string} role - Message role (user, assistant, system, error)
     * @param {string} content - Message content
     * @param {string} displayRole - Display role override
     */
    addMessage(role, content, displayRole = null) {
        const message = {
            role: role,
            content: content,
            timestamp: new Date(),
            displayRole: displayRole || role
        };
        
        this.messages.push(message);
        this._renderMessage(message);
        this._scrollToBottom();
    }

    /**
     * Render a message
     * @private
     */
    async _renderMessage(message) {
        // ✅ FIX #2: Safety check before rendering
        if (!this.messagesContainer) {
            console.error('[ChatUI] Cannot render message, messagesContainer not ready');
            return;
        }
        
        const messageEl = document.createElement('div');
        messageEl.className = `fl-message ${message.role}`;
        
        // Header
        const headerEl = document.createElement('div');
        headerEl.className = 'fl-message-header';
        
        const roleEl = document.createElement('span');
        roleEl.className = 'fl-message-role';
        roleEl.textContent = this._formatRole(message.displayRole);
        
        const timeEl = document.createElement('span');
        timeEl.className = 'fl-message-time';
        timeEl.textContent = this._formatTime(message.timestamp);
        
        headerEl.appendChild(roleEl);
        headerEl.appendChild(timeEl);
        
        // Content
        const contentEl = document.createElement('div');
        contentEl.className = 'fl-message-content';
        
        // Render markdown
        if (message.role !== 'error') {
            const html = marked.parse(message.content);
            contentEl.innerHTML = html;
            
            // Render mermaid diagrams
            const mermaidBlocks = contentEl.querySelectorAll('code.language-mermaid');
            for (const block of mermaidBlocks) {
                await this._renderMermaidDiagram(block);
            }
        } else {
            contentEl.textContent = message.content;
        }
        
        messageEl.appendChild(headerEl);
        messageEl.appendChild(contentEl);
        
        this.messagesContainer.appendChild(messageEl);
    }

    /**
     * Render Mermaid diagram
     * @private
     */
    async _renderMermaidDiagram(codeBlock) {
        const mermaidCode = codeBlock.textContent;
        const id = `mermaid-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
        
        try {
            const { svg } = await mermaid.render(id, mermaidCode);
            
            const container = document.createElement('div');
            container.className = 'fl-mermaid-container';
            container.innerHTML = svg;
            
            // Replace code block with diagram
            const pre = codeBlock.parentElement;
            pre.replaceWith(container);
        } catch (error) {
            console.error('[ChatUI] Mermaid render error:', error);
            // Keep code block on error
        }
    }

    /**
     * Format role for display
     * @private
     */
    _formatRole(role) {
        const roleMap = {
            'user': 'You',
            'assistant': 'Ren',
            'system': 'System',
            'error': 'Error'
        };
        return roleMap[role] || role;
    }

    /**
     * Format timestamp
     * @private
     */
    _formatTime(timestamp) {
        const now = new Date();
        const diff = now - timestamp;
        
        if (diff < 60000) {
            return 'just now';
        } else if (diff < 3600000) {
            const mins = Math.floor(diff / 60000);
            return `${mins}m ago`;
        } else {
            return timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
    }

    /**
     * Set typing indicator
     * @param {boolean} isTyping - Whether assistant is typing
     */
    setTyping(isTyping) {
        this.isTyping = isTyping;
        this.typingIndicator.style.display = isTyping ? 'flex' : 'none';
        if (isTyping) {
            this._scrollToBottom();
        }
    }

    /**
     * Update connection status
     * @private
     */
    _updateConnectionStatus(status) {
        this.statusIndicator.className = `fl-status-indicator ${status}`;
        
        const statusText = {
            'connected': 'Connected',
            'disconnected': 'Disconnected',
            'connecting': 'Connecting...'
        };
        
        this.statusText.textContent = statusText[status] || status;
        
        // Add system message for connection changes
        if (status === 'connected') {
            this.addMessage('system', '✅ Connected to FL_JS backend');
        } else if (status === 'disconnected') {
            this.addMessage('system', '⚠️ Disconnected from backend. Reconnecting...');
        }
    }

    /**
     * Scroll to bottom of messages
     * @private
     */
    _scrollToBottom() {
        setTimeout(() => {
            this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
        }, 100);
    }

    /**
     * Clear all messages
     */
    clearMessages() {
        this.messages = [];
        this.messagesContainer.innerHTML = '';
        this._addWelcomeMessage();
        console.log('[ChatUI] Messages cleared');
    }

    /**
     * Get all messages
     * @returns {Array} Message history
     */
    getMessages() {
        return [...this.messages];
    }

    /**
     * Destroy chat UI
     */
    destroy() {
        // Cleanup tool activity
        try {
            this.toolActivity?.cleanup();
        } catch (error) {
            console.warn('[ChatUI] Could not cleanup tool activity on destroy:', error);
        }
        
        this.container.innerHTML = '';
        console.log('[ChatUI] Destroyed');
    }
}