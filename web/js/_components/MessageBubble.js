/**
 * MessageBubble Component
 *
 * Renders individual chat message bubbles with markdown support
 * and mermaid diagram rendering.
 *
 * @module MessageBubble
 */

import { marked } from "https://cdn.jsdelivr.net/npm/marked@11.1.1/+esm";
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.6.1/+esm";

export class MessageBubble {
    constructor() {
        // Configure marked for safe rendering
        const renderer = new marked.Renderer();

        // Override the default link renderer
        renderer.link = function(href, title, text) {
            const safeTitle = title ? ` title="${title}"` : "";
            return `<a href="${href}"${safeTitle} target="_blank" rel="noopener noreferrer">${text}</a>`;
        };

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
    }

    /**
     * Create a message bubble element
     * @param {Object} message - Message data
     * @param {string} message.role - Message role (user, assistant, system, error)
     * @param {string} message.content - Message content (markdown supported)
     * @param {Date} message.timestamp - Message timestamp
     * @param {string} message.displayRole - Display role override
     * @returns {Promise<HTMLElement>} Message element
     */
    async create(message) {
        const messageEl = document.createElement('div');
        messageEl.className = `fl-message ${message.role}`;

        // Add header (role and timestamp)
        const headerEl = this._createHeader(message);
        messageEl.appendChild(headerEl);

        // Add content (markdown + mermaid)
        const contentEl = await this._createContent(message);
        messageEl.appendChild(contentEl);

        return messageEl;
    }

    /**
     * Create message header with role and timestamp
     * @private
     */
    _createHeader(message) {
        const headerEl = document.createElement('div');
        headerEl.className = 'fl-message-header';

        const roleEl = document.createElement('span');
        roleEl.className = 'fl-message-role';
        roleEl.textContent = this._formatRole(message.displayRole || message.role);

        const timeEl = document.createElement('span');
        timeEl.className = 'fl-message-time';
        timeEl.textContent = this._formatTime(message.timestamp);

        headerEl.appendChild(roleEl);
        headerEl.appendChild(timeEl);

        return headerEl;
    }

    /**
     * Create message content with markdown rendering
     * @private
     */
    async _createContent(message) {
        const contentEl = document.createElement('div');
        contentEl.className = 'fl-message-content';

        // Render markdown for non-error messages
        if (message.role !== 'error') {
            const html = marked.parse(message.content);
            contentEl.innerHTML = html;

            // Render mermaid diagrams
            const mermaidBlocks = contentEl.querySelectorAll('code.language-mermaid');
            for (const block of mermaidBlocks) {
                await this._renderMermaidDiagram(block);
            }
        } else {
            // Plain text for errors
            contentEl.textContent = message.content;
        }

        return contentEl;
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
            console.error('[MessageBubble] Mermaid render error:', error);
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
     * Format timestamp for display
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
}
