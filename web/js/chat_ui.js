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

const PROVIDERS = [
    { id: 'cloud', label: 'Claude' },
    { id: 'openrouter', label: 'Router' },
    { id: 'local', label: 'Local' },
    { id: 'gemini', label: 'Gemini' },
    { id: 'openai', label: 'OpenAI' },
];


/**
 * ChatUI class - Manages chat interface and message rendering
 */
export class ChatUI {
    constructor(container, chatClient, wsClient = null) {
        this.container = container;
        if (wsClient === null && chatClient && typeof chatClient.send === 'function') {
            wsClient = chatClient;
            chatClient = null;
        }
        this.chatClient = chatClient;
        this.wsClient = wsClient;
        this.messages = [];
        this.isTyping = false;
        this.isStreaming = false;
        this.conversations = [];
        this.activeConversationId = localStorage.getItem('fl_ren_conversation_id') || chatClient?.conversationId || null;
        if (this.chatClient && this.activeConversationId) {
            this.chatClient.conversationId = this.activeConversationId;
        }
        this.streamingAssistantEl = null;
        this.streamingAssistantContentEl = null;
        this.streamingAssistantText = '';
        this.providerStatus = null;
        this.localModelOptions = [];
        this.authStep = 'initial';
        this.pendingOAuthState = null;
        this.comfyStatusTimer = null;
        this.renLauncherStatus = null;
        this.renLauncherTimer = null;

        // Initialize message bubble renderer
        this.messageBubble = new MessageBubble();
        
        // Initialize UI
        this._initializeUI();
        this._attachEventHandlers();
        this._loadProviderStatus();
        this._loadConversations();
        this._loadComfyStatus();
        this._loadRenLauncherStatus();
        this.comfyStatusTimer = setInterval(() => this._loadComfyStatus(), 15000);
        this.renLauncherTimer = setInterval(() => this._loadRenLauncherStatus(), 5000);

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
                <div class="fl-chat-header-right">
                    <div class="fl-chat-status">
                        <span class="fl-status-indicator" id="fl-status-indicator"></span>
                        <span class="fl-status-text" id="fl-status-text">Checking...</span>
                    </div>
                    <button class="fl-ren-launch" id="fl-ren-launch" title="Start hidden Ren daemon">Start</button>
                </div>
            </div>
            <div class="fl-provider-bar">
                <div class="fl-provider-toggle" id="fl-provider-toggle"></div>
                <select class="fl-provider-model" id="fl-provider-model" title="Model"></select>
            </div>
            <div class="fl-conversation-bar">
                <select class="fl-conversation-select" id="fl-conversation-select" title="Conversation"></select>
                <button class="fl-conversation-new" id="fl-conversation-new" title="New chat">+</button>
            </div>
            <div class="fl-comfy-bar">
                <span class="fl-comfy-state" id="fl-comfy-state">Comfy: checking...</span>
                <button class="fl-comfy-restart" id="fl-comfy-restart" title="Restart ComfyUI">Restart</button>
            </div>
            <div class="fl-provider-setup" id="fl-provider-setup" style="display: none;"></div>
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
                <button class="fl-chat-stop" id="fl-chat-stop" title="Stop response" style="display: none;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                        <rect x="6" y="6" width="12" height="12"></rect>
                    </svg>
                </button>
            </div>
        `;

        this.container.appendChild(layout);

        // Store references using querySelector on container
        this.messagesContainer = this.container.querySelector('#fl-chat-messages');
        this.inputField = this.container.querySelector('#fl-chat-input');
        this.sendButton = this.container.querySelector('#fl-chat-send');
        this.stopButton = this.container.querySelector('#fl-chat-stop');
        this.typingIndicator = this.container.querySelector('#fl-chat-typing');
        this.statusIndicator = this.container.querySelector('#fl-status-indicator');
        this.statusText = this.container.querySelector('#fl-status-text');
        this.renLaunchButton = this.container.querySelector('#fl-ren-launch');
        this.providerToggle = this.container.querySelector('#fl-provider-toggle');
        this.providerModelSelect = this.container.querySelector('#fl-provider-model');
        this.providerSetup = this.container.querySelector('#fl-provider-setup');
        this.conversationSelect = this.container.querySelector('#fl-conversation-select');
        this.newConversationButton = this.container.querySelector('#fl-conversation-new');
        this.comfyState = this.container.querySelector('#fl-comfy-state');
        this.comfyRestartButton = this.container.querySelector('#fl-comfy-restart');

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
        this.stopButton.addEventListener('click', () => this._cancelStream());
        
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

        this.providerToggle.addEventListener('click', (e) => {
            const button = e.target.closest('[data-provider]');
            if (button) {
                this._selectProvider(button.dataset.provider);
            }
        });

        this.providerModelSelect.addEventListener('change', () => {
            this._selectProviderModel(this.providerModelSelect.value);
        });

        this.conversationSelect.addEventListener('change', () => {
            if (this.conversationSelect.value) {
                this._loadConversation(this.conversationSelect.value);
            }
        });

        this.newConversationButton.addEventListener('click', () => {
            this._createConversation();
        });

        this.comfyRestartButton.addEventListener('click', () => {
            this._restartComfy();
        });

        this.renLaunchButton.addEventListener('click', () => {
            this._toggleRenDaemon();
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
            "Help me install missing nodes and models",
            "Show my latest outputs",
            "Run the workflow a couple times with with variations",
            "My workflow isn't working—can you help debug it?",
            "Lets layout this workflow better",
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
        if (!message || this.isStreaming) return;
        if (this.chatClient && !this._isCurrentProviderReady()) {
            this.addMessage('error', 'Configure the selected provider before sending a message.');
            this._renderProviderSetup();
            return;
        }
        
        // Add user message to UI
        this.addMessage('user', message);
        
        // Clear input only if we're using the input field
        if (!messageText) {
            this.inputField.value = '';
            this.inputField.style.height = 'auto';
        }
        
        this._setStreaming(true);
        this.setTyping(true);
        
        // Send to backend
        try {
            if (this.chatClient) {
                await this.chatClient.sendMessage(message, (event) => this._handleChatEvent(event));
                await this._loadConversations(false);
            } else {
                await this.wsClient.send({
                    type: 'user_message',
                    message: message
                });
            }
        } catch (error) {
            console.error('[ChatUI] Error sending message:', error);
            this.addMessage('error', `Failed to send message: ${error.message}`);
            this.setTyping(false);
            this._setStreaming(false);
            this._finishAssistantStream();
        }
    }

    async _loadConversations(loadActive = true) {
        if (!this.chatClient?.listConversations || !this.conversationSelect) return;
        try {
            this.conversations = await this.chatClient.listConversations();
            this._renderConversationSelect();
            const savedId = localStorage.getItem('fl_ren_conversation_id');
            const target = savedId || this.conversations[0]?.id;
            if (loadActive && target && this.conversations.some((conversation) => conversation.id === target)) {
                await this._loadConversation(target);
            }
        } catch (error) {
            console.warn('[ChatUI] Could not load conversations:', error);
        }
    }

    async _loadRenLauncherStatus() {
        if (!this.renLaunchButton) return;
        try {
            const response = await fetch('/fl_ren/launcher/status');
            if (!response.ok) {
                throw new Error(`Launcher status failed: ${response.status}`);
            }
            const status = await response.json();
            this.renLauncherStatus = status;
            const running = Boolean(status.backendReachable);
            this.renLaunchButton.textContent = running ? 'Stop' : 'Start';
            this.renLaunchButton.title = running ? 'Stop hidden Ren daemon' : 'Start hidden Ren daemon';
            this.renLaunchButton.classList.toggle('running', running);

            if (!running) {
                this._updateConnectionStatus('stopped');
            } else if (this.wsClient && !this.wsClient.getState().connected && !this.wsClient.getState().handshakeComplete) {
                this.wsClient.reconnectAttempts = 0;
                this.wsClient.connect();
            }
        } catch (error) {
            this.renLauncherStatus = null;
            this.renLaunchButton.textContent = 'Start';
            this.renLaunchButton.classList.remove('running');
        }
    }

    async _toggleRenDaemon() {
        if (!this.renLaunchButton) return;
        const running = Boolean(this.renLauncherStatus?.backendReachable);
        const endpoint = running ? '/fl_ren/launcher/stop' : '/fl_ren/launcher/start';
        this.renLaunchButton.disabled = true;
        this.renLaunchButton.textContent = running ? 'Stopping...' : 'Starting...';
        try {
            const response = await fetch(endpoint, { method: 'POST' });
            const status = await response.json();
            if (!response.ok || status.error) {
                throw new Error(status.error || `Launcher request failed: ${response.status}`);
            }
            if (running) {
                this.wsClient?.disconnect();
                this._updateConnectionStatus('stopped');
            } else {
                this.wsClient?.connect();
                await this._loadProviderStatus();
                await this._loadConversations();
                await this._loadComfyStatus();
            }
            await this._loadRenLauncherStatus();
        } catch (error) {
            console.error('[ChatUI] Ren launcher request failed:', error);
            this.addMessage('error', `Ren launcher failed: ${error.message}`);
        } finally {
            this.renLaunchButton.disabled = false;
            await this._loadRenLauncherStatus();
        }
    }

    _renderConversationSelect() {
        if (!this.conversationSelect) return;
        const options = this.conversations.map((conversation) => {
            const label = this._escapeHtml(conversation.title || 'New chat');
            return `<option value="${this._escapeHtml(conversation.id)}">${label}</option>`;
        });
        if (!options.length) {
            options.push('<option value="">New chat</option>');
        }
        this.conversationSelect.innerHTML = options.join('');
        if (this.activeConversationId) {
            this.conversationSelect.value = this.activeConversationId;
        }
    }

    async _createConversation() {
        if (!this.chatClient?.createConversation) return;
        try {
            const conversation = await this.chatClient.createConversation('New chat');
            this.activeConversationId = conversation.id;
            localStorage.setItem('fl_ren_conversation_id', conversation.id);
            this.messages = [];
            this.messagesContainer.innerHTML = '';
            this._addWelcomeMessage();
            await this._loadConversations(false);
        } catch (error) {
            console.error('[ChatUI] Could not create conversation:', error);
            this.addMessage('error', `Failed to create chat: ${error.message}`);
        }
    }

    async _loadConversation(conversationId) {
        if (!this.chatClient?.loadConversation || this.isStreaming) return;
        try {
            const data = await this.chatClient.loadConversation(conversationId);
            this.activeConversationId = conversationId;
            localStorage.setItem('fl_ren_conversation_id', conversationId);
            this._renderLoadedMessages(data.messages || []);
            this._renderConversationSelect();
        } catch (error) {
            console.warn('[ChatUI] Could not load conversation:', error);
        }
    }

    _renderLoadedMessages(messages) {
        this.messages = [];
        this.messagesContainer.innerHTML = '';
        if (!messages.length) {
            this._addWelcomeMessage();
            return;
        }

        for (const item of messages) {
            const message = {
                role: item.role,
                content: item.content,
                timestamp: item.createdAt ? new Date(item.createdAt) : new Date(),
                displayRole: item.role,
            };
            this.messages.push(message);
            this._renderMessage(message);
        }
        this._scrollToBottom();
    }

    async _loadComfyStatus() {
        if (!this.chatClient?.comfyStatus || !this.comfyState) return;
        try {
            const status = await this.chatClient.comfyStatus();
            const reachable = Boolean(status.reachable);
            const mode = status.mode || 'embedded';
            this.comfyState.textContent = `Comfy: ${reachable ? 'online' : 'offline'} (${mode})`;
            this.comfyState.className = `fl-comfy-state ${reachable ? 'online' : 'offline'}`;
            if (this.comfyRestartButton) {
                this.comfyRestartButton.disabled = !status.canManageProcess;
                this.comfyRestartButton.title = status.canManageProcess
                    ? 'Restart ComfyUI'
                    : 'Restart requires Ren daemon mode';
            }
        } catch (error) {
            this.comfyState.textContent = 'Comfy: unknown';
            this.comfyState.className = 'fl-comfy-state offline';
        }
    }

    async _restartComfy() {
        if (!this.chatClient?.restartComfy || !this.comfyRestartButton) return;
        this.comfyRestartButton.disabled = true;
        this.comfyState.textContent = 'Comfy: restarting...';
        try {
            await this.chatClient.restartComfy();
            setTimeout(() => this._loadComfyStatus(), 2000);
        } catch (error) {
            this.addMessage('error', `Failed to restart Comfy: ${error.message}`);
            await this._loadComfyStatus();
        } finally {
            this.comfyRestartButton.disabled = false;
        }
    }

    async _loadProviderStatus() {
        if (!this.chatClient?.providerStatus) return;
        try {
            this.providerStatus = await this.chatClient.providerStatus();
            this._renderProviderControls();
        } catch (error) {
            console.warn('[ChatUI] Could not load provider status:', error);
        }
    }

    _renderProviderControls() {
        if (!this.providerStatus || !this.providerToggle) return;

        const activeProvider = this.providerStatus.provider || 'anthropic';
        this.providerToggle.innerHTML = PROVIDERS.map((provider) => `
            <button
                type="button"
                class="fl-provider-button ${provider.id === activeProvider ? 'active' : ''}"
                data-provider="${provider.id}"
                title="${provider.label}"
            >${provider.label}</button>
        `).join('');

        const modelOptions = this._getModelOptions(activeProvider);
        const currentModel = this._currentModelForProvider(activeProvider);
        this.providerModelSelect.innerHTML = modelOptions.map((model) => `
            <option value="${this._escapeAttr(model.id)}" ${model.id === currentModel ? 'selected' : ''}>${model.label}</option>
        `).join('');
        this.providerModelSelect.style.display = modelOptions.length ? 'block' : 'none';
        this._renderProviderSetup();
    }

    _getModelOptions(provider) {
        if (provider === 'local') {
            const current = this.providerStatus?.providers?.local?.model;
            const options = [...this.localModelOptions];
            if (current && !options.some((model) => model.id === current)) {
                options.unshift({ id: current, label: current });
            }
            return options;
        }
        return this.providerStatus?.modelOptions?.[provider] || [];
    }

    _currentModelForProvider(provider) {
        if (provider === 'local') {
            return this.providerStatus?.providers?.local?.model || '';
        }
        return this.providerStatus?.models?.[provider] || this.providerStatus?.model || '';
    }

    _isCurrentProviderReady() {
        if (!this.providerStatus) return true;
        const provider = this.providerStatus.provider;
        return Boolean(this.providerStatus.providers?.[provider]?.configured);
    }

    async _selectProvider(provider) {
        if (!provider || provider === this.providerStatus?.provider) return;
        try {
            const model = provider === 'local' ? this.providerStatus?.providers?.local?.model : this._currentModelForProvider(provider);
            this.providerStatus = await this.chatClient.selectProvider(provider, model || null);
            this._renderProviderControls();
        } catch (error) {
            this.addMessage('error', `Failed to switch provider: ${error.message}`);
        }
    }

    async _selectProviderModel(model) {
        const provider = this.providerStatus?.provider;
        if (!provider || provider === 'local' || !model) return;
        try {
            this.providerStatus = await this.chatClient.selectProvider(provider, model);
            this._renderProviderControls();
        } catch (error) {
            this.addMessage('error', `Failed to change model: ${error.message}`);
        }
    }

    _renderProviderSetup() {
        if (!this.providerStatus || !this.providerSetup) return;

        const provider = this.providerStatus.provider;
        const status = this.providerStatus.providers?.[provider] || {};
        const shouldShow = provider === 'local' || !status.configured;
        this.providerSetup.style.display = shouldShow ? 'flex' : 'none';

        if (!shouldShow) {
            this.providerSetup.innerHTML = '';
            return;
        }

        if (provider === 'local') {
            this.providerSetup.innerHTML = `
                <input class="fl-provider-input" id="fl-local-url" value="${this._escapeAttr(status.baseURL || 'http://127.0.0.1:1234/v1')}" placeholder="http://127.0.0.1:1234/v1" />
                <div class="fl-provider-row">
                    <input class="fl-provider-input" id="fl-local-model" value="${this._escapeAttr(status.model || '')}" placeholder="model" />
                    <button class="fl-provider-action" id="fl-local-fetch" type="button">Models</button>
                </div>
                <button class="fl-provider-action primary" id="fl-local-save" type="button">Connect</button>
            `;
            this.providerSetup.querySelector('#fl-local-fetch').addEventListener('click', () => this._fetchLocalModels());
            this.providerSetup.querySelector('#fl-local-save').addEventListener('click', () => this._saveLocalProvider());
            return;
        }

        if (provider === 'cloud') {
            const status = this.providerStatus.providers?.cloud || {};
            if (status.authenticated) {
                this.providerSetup.innerHTML = `
                    <div class="fl-provider-row">
                        <span class="fl-provider-note">Claude connected with OAuth.</span>
                        <button class="fl-provider-action" id="fl-cloud-signout" type="button">Sign out</button>
                    </div>
                `;
                this.providerSetup.querySelector('#fl-cloud-signout').addEventListener('click', () => this._signOutClaude());
                return;
            }

            if (this.authStep === 'paste-code') {
                this.providerSetup.innerHTML = `
                    <div class="fl-provider-note">A browser window opened. Paste the authorization code here.</div>
                    <div class="fl-provider-row">
                        <input class="fl-provider-input" id="fl-cloud-code" placeholder="Paste code here..." />
                        <button class="fl-provider-action primary" id="fl-cloud-submit" type="button">Submit</button>
                    </div>
                    <button class="fl-provider-action" id="fl-cloud-cancel" type="button">Cancel</button>
                `;
                this.providerSetup.querySelector('#fl-cloud-submit').addEventListener('click', () => this._submitClaudeCode());
                this.providerSetup.querySelector('#fl-cloud-cancel').addEventListener('click', () => {
                    this.authStep = 'initial';
                    this._renderProviderSetup();
                });
                return;
            }

            if (this.authStep === 'exchanging') {
                this.providerSetup.innerHTML = `<div class="fl-provider-note">Authenticating...</div>`;
                return;
            }

            this.providerSetup.innerHTML = `
                <div class="fl-provider-note">Sign in with your Anthropic account to use Claude.</div>
                <button class="fl-provider-action primary" id="fl-cloud-connect" type="button">Connect with Claude</button>
            `;
            this.providerSetup.querySelector('#fl-cloud-connect').addEventListener('click', () => this._startClaudeOAuth());
            return;
        }

        const label = PROVIDERS.find((item) => item.id === provider)?.label || provider;
        this.providerSetup.innerHTML = `
            <div class="fl-provider-row">
                <input class="fl-provider-input" id="fl-provider-key" type="password" placeholder="${label} API key" />
                <button class="fl-provider-action primary" id="fl-provider-save-key" type="button">Save</button>
            </div>
        `;
        this.providerSetup.querySelector('#fl-provider-save-key').addEventListener('click', () => this._saveProviderKey());
    }

    async _saveProviderKey() {
        const input = this.providerSetup.querySelector('#fl-provider-key');
        const apiKey = input?.value?.trim();
        if (!apiKey) return;
        try {
            this.providerStatus = await this.chatClient.setProviderKey(this.providerStatus.provider, apiKey);
            this._renderProviderControls();
        } catch (error) {
            this.addMessage('error', `Failed to save provider key: ${error.message}`);
        }
    }

    async _startClaudeOAuth() {
        try {
            const response = await fetch(`${this.chatClient.baseUrl}/api/auth/start-oauth`);
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.message || data.error || 'Failed to start OAuth');
            }
            this.pendingOAuthState = data.state;
            window.open(data.authorizationUrl, '_blank');
            this.authStep = 'paste-code';
            this._renderProviderSetup();
        } catch (error) {
            this.addMessage('error', `Failed to start Claude login: ${error.message}`);
        }
    }

    async _submitClaudeCode() {
        const input = this.providerSetup.querySelector('#fl-cloud-code');
        const pasted = input?.value?.trim();
        if (!pasted || !this.pendingOAuthState) return;

        const parts = pasted.split('#');
        const code = parts[0].trim();
        const callbackState = parts[1]?.trim() || '';
        this.authStep = 'exchanging';
        this._renderProviderSetup();

        try {
            const response = await fetch(`${this.chatClient.baseUrl}/api/auth/exchange-code`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    code,
                    callbackState,
                    state: this.pendingOAuthState,
                }),
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.message || data.error || 'Authentication failed');
            }
            this.pendingOAuthState = null;
            this.authStep = 'initial';
            this.providerStatus = await this.chatClient.providerStatus();
            this._renderProviderControls();
        } catch (error) {
            this.authStep = 'paste-code';
            this.addMessage('error', `Claude login failed: ${error.message}`);
            this._renderProviderSetup();
        }
    }

    async _signOutClaude() {
        try {
            const response = await fetch(`${this.chatClient.baseUrl}/api/auth/sign-out`, {
                method: 'POST',
            });
            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.message || data.error || 'Failed to sign out');
            }
            this.providerStatus = await this.chatClient.providerStatus();
            this._renderProviderControls();
        } catch (error) {
            this.addMessage('error', `Failed to sign out of Claude: ${error.message}`);
        }
    }

    async _fetchLocalModels() {
        const baseURL = this.providerSetup.querySelector('#fl-local-url')?.value?.trim();
        if (!baseURL) return;
        try {
            this.localModelOptions = await this.chatClient.listLocalModels(baseURL);
            const modelInput = this.providerSetup.querySelector('#fl-local-model');
            if (modelInput && this.localModelOptions.length && !modelInput.value.trim()) {
                modelInput.value = this.localModelOptions[0].id;
            }
            this._renderProviderControls();
        } catch (error) {
            this.addMessage('error', `Failed to fetch local models: ${error.message}`);
        }
    }

    async _saveLocalProvider() {
        const baseURL = this.providerSetup.querySelector('#fl-local-url')?.value?.trim();
        const model = this.providerSetup.querySelector('#fl-local-model')?.value?.trim();
        if (!baseURL || !model) return;
        try {
            await this.chatClient.setLocalConfig(baseURL, model);
            this.providerStatus = await this.chatClient.selectProvider('local', model);
            this._renderProviderControls();
        } catch (error) {
            this.addMessage('error', `Failed to connect local model: ${error.message}`);
        }
    }

    _escapeAttr(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    /**
     * Handle REST/SSE chat event from the backend.
     * @private
     */
    _handleChatEvent(data) {
        switch (data.type) {
            case 'conversation_id':
                this.activeConversationId = data.id;
                localStorage.setItem('fl_ren_conversation_id', data.id);
                if (this.chatClient) {
                    this.chatClient.conversationId = data.id;
                }
                break;
            case 'block_start':
                if (data.blockType === 'text') {
                    this.setTyping(false);
                    this._startAssistantStream();
                }
                break;
            case 'text_delta':
                this.setTyping(false);
                this._appendAssistantStream(data.text || '');
                break;
            case 'block_stop':
                break;
            case 'done':
                this.setTyping(false);
                this._setStreaming(false);
                this._finishAssistantStream();
                break;
            case 'error':
                this.setTyping(false);
                this._setStreaming(false);
                this._finishAssistantStream();
                this.addMessage('error', data.message || 'An error occurred');
                break;
            default:
                break;
        }
    }

    /**
     * Cancel the active REST/SSE response.
     * @private
     */
    async _cancelStream() {
        if (!this.chatClient || !this.isStreaming) return;
        try {
            await this.chatClient.cancel(this.activeConversationId);
        } catch (error) {
            console.error('[ChatUI] Error cancelling stream:', error);
            this.addMessage('error', `Failed to cancel response: ${error.message}`);
        } finally {
            this.setTyping(false);
            this._setStreaming(false);
            this._finishAssistantStream();
        }
    }

    /**
     * Handle agent response
     * @private
     */
    _handleAgentResponse(data) {
        this.setTyping(false);
        this._setStreaming(false);
        
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
        this._setStreaming(false);
        this.addMessage('error', data.error || 'An error occurred');
    }

    /**
     * Create a streaming assistant message if one is not active.
     * @private
     */
    _startAssistantStream() {
        if (this.streamingAssistantEl) return;

        this.streamingAssistantText = '';
        const messageEl = document.createElement('div');
        messageEl.className = 'fl-message assistant streaming';

        const headerEl = document.createElement('div');
        headerEl.className = 'fl-message-header';
        headerEl.innerHTML = `
            <span class="fl-message-role">Ren</span>
            <span class="fl-message-time">now</span>
        `;

        const contentEl = document.createElement('div');
        contentEl.className = 'fl-message-content';

        messageEl.appendChild(headerEl);
        messageEl.appendChild(contentEl);
        this.messagesContainer.appendChild(messageEl);

        this.streamingAssistantEl = messageEl;
        this.streamingAssistantContentEl = contentEl;
        this._scrollToBottom();
    }

    /**
     * Append streamed assistant text.
     * @private
     */
    _appendAssistantStream(text) {
        if (!text) return;
        this._startAssistantStream();
        this.streamingAssistantText += text;
        this.streamingAssistantContentEl.innerHTML = this._renderMarkdown(this.streamingAssistantText);
        this._scrollToBottom();
    }

    /**
     * Finalize the active streaming assistant message.
     * @private
     */
    _finishAssistantStream() {
        if (!this.streamingAssistantEl) return;
        if (this.streamingAssistantText.trim()) {
            const message = {
                role: 'assistant',
                content: this.streamingAssistantText,
                timestamp: new Date(),
                displayRole: 'assistant'
            };
            const streamingEl = this.streamingAssistantEl;
            this.messages.push(message);
            this.messageBubble.create(message)
                .then((messageEl) => {
                    if (streamingEl.parentNode) {
                        streamingEl.replaceWith(messageEl);
                    }
                    this._scrollToBottom();
                })
                .catch((error) => {
                    console.error('[ChatUI] Failed to render final assistant message:', error);
                    streamingEl.classList.remove('streaming');
                });
        } else if (this.streamingAssistantEl.parentNode) {
            this.streamingAssistantEl.parentNode.removeChild(this.streamingAssistantEl);
        }
        this.streamingAssistantEl = null;
        this.streamingAssistantContentEl = null;
        this.streamingAssistantText = '';
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
        const escaped = String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');

        return escaped
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            .replace(/\n/g, '<br>');
    }

    _escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
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
     * Toggle stream controls.
     * @private
     */
    _setStreaming(isStreaming) {
        this.isStreaming = isStreaming;
        if (this.sendButton) {
            this.sendButton.style.display = isStreaming ? 'none' : 'flex';
            this.sendButton.disabled = isStreaming;
        }
        if (this.stopButton) {
            this.stopButton.style.display = isStreaming ? 'flex' : 'none';
        }
        if (this.inputField) {
            this.inputField.disabled = isStreaming;
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
            'connecting': 'Connecting...',
            'stopped': 'Stopped'
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
        if (this.comfyStatusTimer) {
            clearInterval(this.comfyStatusTimer);
            this.comfyStatusTimer = null;
        }
        if (this.renLauncherTimer) {
            clearInterval(this.renLauncherTimer);
            this.renLauncherTimer = null;
        }
        console.log('[ChatUI] Destroyed');
    }
}
