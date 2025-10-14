/**
 * Session Manager for FL_JS Agentic System
 * 
 * Manages session lifecycle:
 * - Generates unique session ID on first load
 * - Stores session ID in localStorage for persistence
 * - Provides session ID to WebSocket client
 * - Handles session clearing/reset
 */

class SessionManager {
    constructor() {
        this.STORAGE_KEY = 'fl_js_session_id';
        this.sessionId = this.getOrCreateSessionId();
        console.log('[SessionManager] Initialized with session ID:', this.sessionId);
    }

    /**
     * Get existing session ID from localStorage or create new one
     * @returns {string} Session ID (UUID v4)
     */
    getOrCreateSessionId() {
        let sessionId = localStorage.getItem(this.STORAGE_KEY);
        
        if (!sessionId) {
            sessionId = this.generateUUID();
            localStorage.setItem(this.STORAGE_KEY, sessionId);
            console.log('[SessionManager] Created new session:', sessionId);
        } else {
            console.log('[SessionManager] Retrieved existing session:', sessionId);
        }
        
        return sessionId;
    }

    /**
     * Generate UUID v4
     * @returns {string} UUID v4 string
     */
    generateUUID() {
        // UUID v4 format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    /**
     * Get current session ID
     * @returns {string} Current session ID
     */
    getSessionId() {
        return this.sessionId;
    }

    /**
     * Clear current session and generate new one
     * This will create a fresh conversation context
     */
    clearSession() {
        const oldSessionId = this.sessionId;
        localStorage.removeItem(this.STORAGE_KEY);
        this.sessionId = this.generateUUID();
        localStorage.setItem(this.STORAGE_KEY, this.sessionId);
        console.log('[SessionManager] Session cleared:', oldSessionId, '->', this.sessionId);
        return this.sessionId;
    }

    /**
     * Check if session exists in localStorage
     * @returns {boolean} True if session exists
     */
    hasExistingSession() {
        return localStorage.getItem(this.STORAGE_KEY) !== null;
    }

    /**
     * Get session metadata
     * @returns {object} Session metadata
     */
    getSessionMetadata() {
        return {
            sessionId: this.sessionId,
            isExisting: this.hasExistingSession(),
            storageKey: this.STORAGE_KEY,
        };
    }
}

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = SessionManager;
}
