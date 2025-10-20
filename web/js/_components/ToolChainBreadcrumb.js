/**
 * ToolChainBreadcrumb - Process indicator showing tool execution chain
 *
 * Displays a breadcrumb trail with three states:
 * - Completed: Checkmark, full color
 * - Loading: Pulsing glow animation
 * - Pending: Dimmed, waiting
 *
 * @module ToolChainBreadcrumb
 */

export class ToolChainBreadcrumb {
    constructor(container, options = {}) {
        this.container = container;
        this.options = {
            maxVisible: options.maxVisible || 4,
            autoHide: options.autoHide !== false,
            autoHideDelay: options.autoHideDelay || 3000,
            ...options
        };

        this.steps = [];
        this.element = null;
        this.hideTimeout = null;

        this._injectStyles();
        this._createBreadcrumb();

        console.log('[ToolChainBreadcrumb] Initialized');
    }

    /**
     * Inject CSS styles for the breadcrumb
     * @private
     */
    _injectStyles() {
        if (document.getElementById('fl-toolchain-styles')) return;

        const style = document.createElement('style');
        style.id = 'fl-toolchain-styles';
        style.textContent = `
            .fl-toolchain-container {
                position: absolute;
                bottom: 100%;
                left: 0;
                right: 0;
                padding: 0 16px 12px 16px;
                pointer-events: none;
                z-index: 100;
                display: flex;
                justify-content: center;
                opacity: 0;
                transform: translateY(10px);
                transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
            }

            .fl-toolchain-container.visible {
                opacity: 1;
                transform: translateY(0);
            }

            .fl-toolchain-breadcrumb {
                display: flex;
                align-items: center;
                gap: 12px;
                background: rgba(26, 26, 26, 0.95);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 107, 53, 0.3);
                border-radius: 30px;
                padding: 10px 20px;
                box-shadow:
                    0 8px 32px rgba(0, 0, 0, 0.4),
                    inset 0 1px 0 rgba(255, 255, 255, 0.1);
                pointer-events: auto;
            }

            .fl-toolchain-crumb {
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 13px;
                color: #e0e0e0;
                padding: 6px 14px;
                border-radius: 16px;
                position: relative;
                overflow: hidden;
                font-weight: 500;
                transition: all 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);
                white-space: nowrap;
            }

            .fl-toolchain-icon {
                font-size: 15px;
                position: relative;
                z-index: 2;
            }

            .fl-toolchain-text {
                position: relative;
                z-index: 2;
                font-size: 13px;
            }

            .fl-toolchain-check {
                color: #4caf50;
                font-size: 14px;
                margin-left: 2px;
                display: none;
            }

            .fl-toolchain-separator {
                color: rgba(255, 107, 53, 0.4);
                font-size: 14px;
                user-select: none;
            }

            /* State: Pending */
            .fl-toolchain-crumb.pending {
                background: rgba(30, 30, 30, 0.5);
                opacity: 0.5;
            }

            /* State: Loading (Active) - Pulsing Glow */
            .fl-toolchain-crumb.loading {
                background: rgba(255, 107, 53, 0.15);
                border: 1px solid rgba(255, 107, 53, 0.3);
                animation: fl-pulse-glow 1.5s ease-in-out infinite;
            }

            @keyframes fl-pulse-glow {
                0%, 100% {
                    box-shadow: 0 0 0 rgba(255, 107, 53, 0);
                    border-color: rgba(255, 107, 53, 0.3);
                }
                50% {
                    box-shadow: 0 0 20px rgba(255, 107, 53, 0.4);
                    border-color: rgba(255, 107, 53, 0.6);
                }
            }

            /* State: Complete */
            .fl-toolchain-crumb.completed {
                background: linear-gradient(135deg, rgba(255, 107, 53, 0.25), rgba(196, 69, 54, 0.25));
                box-shadow: 0 4px 12px rgba(255, 107, 53, 0.2);
            }

            .fl-toolchain-crumb.completed .fl-toolchain-check {
                display: inline;
            }

            /* Entrance animation for crumbs */
            .fl-toolchain-crumb {
                animation: fl-crumb-appear 0.4s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
                opacity: 0;
                transform: scale(0.8) translateY(-10px);
            }

            @keyframes fl-crumb-appear {
                to {
                    opacity: 1;
                    transform: scale(1) translateY(0);
                }
            }

            .fl-toolchain-crumb:nth-child(1) { animation-delay: 0s; }
            .fl-toolchain-crumb:nth-child(3) { animation-delay: 0.05s; }
            .fl-toolchain-crumb:nth-child(5) { animation-delay: 0.1s; }
            .fl-toolchain-crumb:nth-child(7) { animation-delay: 0.15s; }
        `;

        document.head.appendChild(style);
    }

    /**
     * Create the breadcrumb container
     * @private
     */
    _createBreadcrumb() {
        // Create container
        this.element = document.createElement('div');
        this.element.className = 'fl-toolchain-container';

        // Create breadcrumb element
        this.breadcrumb = document.createElement('div');
        this.breadcrumb.className = 'fl-toolchain-breadcrumb';

        this.element.appendChild(this.breadcrumb);

        // Insert into container
        this.container.appendChild(this.element);
    }

    /**
     * Add or update a step in the chain
     * @param {string} id - Unique identifier for the step
     * @param {string} icon - Emoji icon
     * @param {string} label - Label text
     * @param {string} state - State: 'pending', 'loading', 'completed'
     */
    setStep(id, icon, label, state = 'pending') {
        // Find existing step
        let step = this.steps.find(s => s.id === id);

        if (!step) {
            // Create new step
            step = { id, icon, label, state };
            this.steps.push(step);
        } else {
            // Update existing step
            step.icon = icon;
            step.label = label;
            step.state = state;
        }

        // Trim to max visible
        if (this.steps.length > this.options.maxVisible) {
            this.steps = this.steps.slice(-this.options.maxVisible);
        }

        this._render();
        this.show();

        // Auto-hide if all completed
        if (this.options.autoHide && this._allCompleted()) {
            this._scheduleHide();
        }
    }

    /**
     * Start a new tool (add as loading)
     * @param {string} toolName - Tool identifier
     * @param {string} icon - Emoji icon
     * @param {string} label - Display label
     */
    startTool(toolName, icon, label) {
        // Mark previous loading as completed
        this.steps.forEach(step => {
            if (step.state === 'loading') {
                step.state = 'completed';
            }
        });

        this.setStep(toolName, icon, label, 'loading');
    }

    /**
     * Complete a tool
     * @param {string} toolName - Tool identifier
     */
    completeTool(toolName) {
        const step = this.steps.find(s => s.id === toolName);
        if (step) {
            step.state = 'completed';
            this._render();

            if (this.options.autoHide && this._allCompleted()) {
                this._scheduleHide();
            }
        }
    }

    /**
     * Render the breadcrumb
     * @private
     */
    _render() {
        this.breadcrumb.innerHTML = '';

        this.steps.forEach((step, index) => {
            // Add separator
            if (index > 0) {
                const separator = document.createElement('span');
                separator.className = 'fl-toolchain-separator';
                separator.textContent = '›';
                this.breadcrumb.appendChild(separator);
            }

            // Add crumb
            const crumb = document.createElement('div');
            crumb.className = `fl-toolchain-crumb ${step.state}`;
            crumb.innerHTML = `
                <span class="fl-toolchain-icon">${step.icon}</span>
                <span class="fl-toolchain-text">${step.label}</span>
                <span class="fl-toolchain-check">✓</span>
            `;

            this.breadcrumb.appendChild(crumb);
        });
    }

    /**
     * Check if all steps are completed
     * @private
     */
    _allCompleted() {
        return this.steps.length > 0 && this.steps.every(s => s.state === 'completed');
    }

    /**
     * Schedule auto-hide
     * @private
     */
    _scheduleHide() {
        if (this.hideTimeout) {
            clearTimeout(this.hideTimeout);
        }

        this.hideTimeout = setTimeout(() => {
            this.hide();
        }, this.options.autoHideDelay);
    }

    /**
     * Show the breadcrumb
     */
    show() {
        if (this.hideTimeout) {
            clearTimeout(this.hideTimeout);
            this.hideTimeout = null;
        }

        this.element.classList.add('visible');
    }

    /**
     * Hide the breadcrumb
     */
    hide() {
        this.element.classList.remove('visible');
    }

    /**
     * Clear all steps
     */
    clear() {
        this.steps = [];
        this._render();
        this.hide();
    }

    /**
     * Destroy the breadcrumb
     */
    destroy() {
        if (this.hideTimeout) {
            clearTimeout(this.hideTimeout);
        }

        if (this.element && this.element.parentNode) {
            this.element.parentNode.removeChild(this.element);
        }

        console.log('[ToolChainBreadcrumb] Destroyed');
    }
}
