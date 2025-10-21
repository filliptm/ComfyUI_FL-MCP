/**
 * Chat UI - Native ComfyUI sidebar chat interface
 * 
 * This module provides a chat interface that integrates with ComfyUI's native
 * sidebar system. It handles message display, user input, markdown rendering,
 * and Mermaid diagram rendering.
 * 
 * @module chat_ui
 */

import { MessageBubble } from './_components/MessageBubble.js';


/**
 * ChatUI class - Manages chat interface and message rendering
 */
export class ChatUI {
    constructor(container, wsClient) {
        this.container = container;
        this.wsClient = wsClient;
        this.messages = [];
        this.isTyping = false;

        // Initialize message bubble renderer
        this.messageBubble = new MessageBubble();
        
        // Initialize UI
        this._initializeUI();
        this._attachEventHandlers();

        // Track active tool chain for breadcrumb display
        this.activeToolChain = null; // Current tool chain message element
        this.currentToolChain = []; // Array of {name, icon, label, status}

        // Make ChatUI globally available
        if (window.FL_JS) {
            window.FL_JS.chatUI = this;
        } else {
            window.FL_JS = { chatUI: this };
        }

        console.log('[ChatUI] Initialized with tool activity and breadcrumb chain');
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

        // Add debug styles for tool activity
        const debugStyle = document.createElement('style');
        debugStyle.textContent = `
          .fl-chat-layout {
            overflow: visible !important;
            min-height: 100px; /* Ensure space for tool cards */
          }
          .fl-tool-activity-overlay {
            bottom: 8em !important; /* Fixed position above input */
            max-height: 60vh; /* Prevent overflow */
          }
        `;
        document.head.appendChild(debugStyle);
        
        // Verify DOM is ready before adding welcome message
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

            // Check current connection state and update UI immediately
            // This handles the race condition where connection happens before listeners attach
            const state = this.wsClient.getState();
            if (state.connected && state.handshakeComplete) {
                this._updateConnectionStatus('connected');
            } else if (state.connected && !state.handshakeComplete) {
                this._updateConnectionStatus('connecting');
            } else {
                // Keep default "Connecting..." state from initialization
                this._updateConnectionStatus('connecting');
            }
        }

        // Handle ren:// link clicks (add to _attachEventHandlers method)
        this.messagesContainer.addEventListener('click', (e) => {
            const renLink = e.target.closest('.ren-link');
            if (renLink) {
                e.preventDefault();
                const protocol = renLink.dataset.protocol;
                const text = renLink.dataset.text;
                
                if (protocol === 'message' && text) {
                    this._sendMessage(text);
                }
            }
        });
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
            "What models and node packs are missing from this workflow?",
            "My workflow isn't working—can you help debug it?",
            "How can I organize my nodes better?",
            "Analyze the prompts in the workflow",
            "Make something cool"
        ];

        // Accordion header
        const accordionHeader = document.createElement('div');
        accordionHeader.className = 'fl-accordion-header';
        accordionHeader.innerHTML = `<span>💭 Quick start suggestions</span><span class="fl-accordion-arrow">▼</span>`;

        // Accordion content (collapsed by default)
        const accordionContent = document.createElement('div');
        accordionContent.className = 'fl-accordion-content';
        accordionContent.style.display = 'none';

        starterQuestions.forEach(question => {
            const questionEl = document.createElement('div');
            questionEl.className = 'fl-accordion-option';
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
            accordionContent.appendChild(questionEl);
        });

        // Toggle accordion on header click
        accordionHeader.addEventListener('click', () => {
            const isOpen = accordionContent.style.display !== 'none';
            accordionContent.style.display = isOpen ? 'none' : 'flex';
            const arrow = accordionHeader.querySelector('.fl-accordion-arrow');
            arrow.textContent = isOpen ? '▼' : '▲';
        });

        contentEl.appendChild(accordionHeader);
        contentEl.appendChild(accordionContent);
        messageEl.appendChild(contentEl);

        this.messagesContainer.appendChild(messageEl);
    }

    /**
     * Send user message
     * @param {string} messageText - Optional message text (if not provided, uses input field)
     * @private
     */
    async _sendMessage(messageText = null) {
        const message = messageText || this.inputField.value.trim();
        if (!message) return;
        
        // Add user message to UI
        this.addMessage('user', message);
        
        // Clear input only if we're using the input field
        if (!messageText) {
            this.inputField.value = '';
            this.inputField.style.height = 'auto';
        }
        
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
     * Add system message with Ren links (for PWA notifications)
     * @param {Object} messageData - Message data containing content and renLinks
     */
    addSystemMessage(messageData) {
        const { content, renLinks = [] } = messageData;
        
        // Create message element
        const messageEl = document.createElement('div');
        messageEl.className = 'fl-message system-notification';
        
        const contentEl = document.createElement('div');
        contentEl.className = 'fl-message-content';
        
        // Add markdown content
        const textEl = document.createElement('div');
        textEl.className = 'system-notification-text';
        textEl.innerHTML = this._renderMarkdown(content);
        contentEl.appendChild(textEl);
        
        // Add Ren links if present
        if (renLinks.length > 0) {
            const linksContainer = document.createElement('div');
            linksContainer.className = 'ren-links-container';
            
            renLinks.forEach(link => {
                const linkEl = document.createElement('a');
                linkEl.className = 'ren-link';
                linkEl.href = '#';
                linkEl.dataset.protocol = 'message';
                linkEl.dataset.text = link.action;
                linkEl.textContent = link.text;
                linksContainer.appendChild(linkEl);
            });
            
            contentEl.appendChild(linksContainer);
        }
        
        messageEl.appendChild(contentEl);
        this.messagesContainer.appendChild(messageEl);
        this._scrollToBottom();
    }

    /**
     * Simple markdown renderer for system messages
     * @private
     */
    _renderMarkdown(text) {
        return text
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            .replace(/\n/g, '<br>');
    }

    /**
     * Start a new tool in the breadcrumb chain
     * @param {string} toolName - Name of the tool being executed
     * @param {string} icon - Icon for the tool
     * @param {string} label - Short label for the tool
     */
    startToolInChain(toolName, icon, label) {
        if (!this.messagesContainer) {
            console.error('[ChatUI] Cannot start tool chain, messagesContainer not ready');
            return;
        }

        // Check if we need to create a new chain message
        if (!this.activeToolChain) {
            this._createToolChainMessage();
        }

        // Add tool to current chain
        this.currentToolChain.push({
            name: toolName,
            icon: icon,
            label: label,
            status: 'loading' // loading, completed
        });

        // Update the breadcrumb display
        this._updateToolChainDisplay();
    }

    /**
     * Mark a tool as complete in the breadcrumb chain
     * @param {string} toolName - Name of the tool that completed
     */
    completeToolInChain(toolName) {
        // Find the first LOADING tool with this name (not the first overall)
        const tool = this.currentToolChain.find(t => t.name === toolName && t.status === 'loading');
        if (tool) {
            tool.status = 'completed';
            this._updateToolChainDisplay();
        }

        // Check if all tools are complete
        const allComplete = this.currentToolChain.every(t => t.status === 'completed');
        if (allComplete && this.currentToolChain.length > 0) {
            // Finalize the chain after a short delay
            setTimeout(() => {
                this._finalizeToolChain();
            }, 500);
        }
    }

    /**
     * Create a new tool chain message in chat
     * @private
     */
    _createToolChainMessage() {
        const messageEl = document.createElement('div');
        messageEl.className = 'fl-message tool-chain';

        // Content only (no header)
        const contentEl = document.createElement('div');
        contentEl.className = 'fl-message-content';

        const breadcrumbContainer = document.createElement('div');
        breadcrumbContainer.className = 'fl-toolchain-breadcrumb';
        contentEl.appendChild(breadcrumbContainer);

        messageEl.appendChild(contentEl);

        this.messagesContainer.appendChild(messageEl);
        this.activeToolChain = messageEl;

        this._scrollToBottom();
    }

    /**
     * Update the breadcrumb trail display
     * @private
     */
    _updateToolChainDisplay() {
        if (!this.activeToolChain) return;

        const breadcrumbContainer = this.activeToolChain.querySelector('.fl-toolchain-breadcrumb');
        if (!breadcrumbContainer) return;

        // Render breadcrumb trail
        breadcrumbContainer.innerHTML = this.currentToolChain.map(tool => {
            const statusClass = tool.status === 'loading' ? 'loading' : 'completed';
            return `
                <div class="fl-toolchain-crumb ${statusClass}" data-tool="${tool.name}">
                    <span class="fl-crumb-icon">${tool.icon}</span>
                    <span class="fl-crumb-label">${tool.label}</span>
                </div>
            `;
        }).join('');

        this._scrollToBottom();
    }

    /**
     * Finalize the tool chain (all tools complete)
     * @private
     */
    _finalizeToolChain() {
        // Reset for next chain
        this.activeToolChain = null;
        this.currentToolChain = [];
    }

    /**
     * Clear active tool chain on error/disconnect
     */
    clearToolChain() {
        if (this.activeToolChain) {
            this.activeToolChain.classList.add('fl-message-fade-out');
            setTimeout(() => {
                if (this.activeToolChain && this.activeToolChain.parentNode) {
                    this.activeToolChain.parentNode.removeChild(this.activeToolChain);
                }
                this.activeToolChain = null;
                this.currentToolChain = [];
            }, 300);
        }
    }

    /**
     * Render a message using MessageBubble component
     * @private
     */
    async _renderMessage(message) {
        // Safety check before rendering
        if (!this.messagesContainer) {
            console.error('[ChatUI] Cannot render message, messagesContainer not ready');
            return;
        }

        // Use MessageBubble component to create the message element
        const messageEl = await this.messageBubble.create(message);
        this.messagesContainer.appendChild(messageEl);
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