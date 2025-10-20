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
        
        // Add viewport verification
        requestAnimationFrame(() => {
            const rect = card.getBoundingClientRect();
            if (rect.top < 0 || rect.bottom > window.innerHeight) {
                console.warn('[ToolActivity] Card out of viewport:', rect);
                this._adjustCardPosition(card);
            }
        });
    }

    /**
     * Adjust card position if out of viewport
     * @private
     */
    _adjustCardPosition(card) {
        const rect = card.getBoundingClientRect();
        if (rect.bottom > window.innerHeight) {
            card.style.marginBottom = `${rect.bottom - window.innerHeight + 20}px`;
        }
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
        
        // Insert below chat content instead of above input
        const chatContent = this.chatContainer.querySelector('.fl-chat-content');
        if (chatContent) {
            chatContent.appendChild(this.overlay);
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