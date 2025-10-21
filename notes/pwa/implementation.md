# PWA Implementation Plan

This document provides a complete implementation guide for adding PWA support to FL_JS. Follow these steps in order for a smooth implementation.

## Prerequisites

- FL_JS backend and frontend working
- ComfyUI with FL_JS extension installed
- Basic understanding of PWA concepts

## Implementation Steps

### Phase 1: Backend Changes

#### 1.1 Add Static File Serving

**File**: `backend/server.py`

**Changes**:
```python
# Add imports at top
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

# After CORS middleware setup, before routes
# Get project root
PROJECT_ROOT = Path(__file__).parent.parent
PWA_DIR = PROJECT_ROOT / "web" / "pwa"

# Serve PWA static files
app.mount("/pwa/static", StaticFiles(directory=str(PWA_DIR)), name="pwa_static")

# Add PWA route before @app.get("/")
@app.get("/pwa")
@app.get("/pwa/")
async def serve_pwa() -> FileResponse:
    """Serve PWA application."""
    return FileResponse(str(PWA_DIR / "index.html"))
```

#### 1.2 Add Session List API Endpoint

**File**: `backend/server.py`

**Add after health endpoint**:
```python
@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    """List active sessions for PWA session picker.
    
    Returns:
        Dict with sessions list containing session_id, connections, and last_activity
    """
    sessions = []
    for session_id, context in manager.session_contexts.items():
        sessions.append({
            "session_id": session_id,
            "connections": manager.get_connection_info(session_id),
            "last_activity": context.last_activity.isoformat(),
            "has_frontend": manager.has_connection(session_id, 'frontend'),
            "has_pwa": manager.has_connection(session_id, 'pwa'),
        })
    
    return {
        "sessions": sessions,
        "total": len(sessions),
    }
```

#### 1.3 Update Connection Type Detection

**File**: `backend/server.py`

**In `websocket_endpoint` function, update connection type detection**:

```python
# Replace existing detection logic (around line 150)
# Detect connection type from client_version
if handshake.client_version:
    version_lower = handshake.client_version.lower()
    if 'mcp' in version_lower:
        connection_type = 'mcp'
    elif 'pwa' in version_lower:
        connection_type = 'pwa'
    else:
        connection_type = 'frontend'
else:
    connection_type = 'frontend'

logger.info(f"Detected connection type: {connection_type}")
```

#### 1.4 Update Tool Request Routing

**File**: `backend/server.py`

**In `handle_user_message` function, after getting response from agent**:

```python
# After: response = await agent.run(...)
# Before: message_history.clear()

# Determine which connections should receive the response
# PWA and frontend should both see agent responses
# but tool_requests should only go to frontend (ComfyUI)
response_targets = []
if manager.has_connection(session_id, 'pwa'):
    response_targets.append('pwa')
if manager.has_connection(session_id, 'frontend'):
    response_targets.append('frontend')

# Set History (Mutable)
message_history.clear()
message_history.extend(response.all_messages())

# Send response to all connected clients
for target in response_targets:
    await manager.send_message(session_id, {
        "type": "agent_response",
        "session_id": session_id,
        "message": response.output,
        "tool_calls": [],
        "is_final": True,
    }, target=target)

logger.info(f"Agent response sent to {session_id} (targets: {response_targets})")
```

**Note**: The existing `route_tool_request_to_frontend` function already handles routing tool requests to the frontend connection, so tool execution will work correctly.

---

### Phase 2: PWA Frontend Structure

#### 2.1 Create Directory Structure

```bash
mkdir -p web/pwa/icons
```

#### 2.2 Create `web/pwa/index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#2d1f3d">
    <meta name="description" content="Ren - Your AI assistant for ComfyUI workflows">
    
    <!-- PWA Manifest -->
    <link rel="manifest" href="/pwa/static/manifest.json">
    
    <!-- Icons -->
    <link rel="icon" type="image/png" sizes="192x192" href="/pwa/static/icons/icon-192.png">
    <link rel="icon" type="image/png" sizes="512x512" href="/pwa/static/icons/icon-512.png">
    <link rel="apple-touch-icon" href="/pwa/static/icons/icon-192.png">
    
    <!-- Styles -->
    <link rel="stylesheet" href="/pwa/static/styles.css">
    
    <title>Ren - ComfyUI Assistant</title>
</head>
<body>
    <div id="app">
        <!-- Session Picker (shown initially) -->
        <div id="session-picker" class="session-picker">
            <div class="picker-header">
                <h1>Ren 🌸</h1>
                <p>Select a ComfyUI session to connect</p>
            </div>
            
            <div id="session-list" class="session-list">
                <!-- Sessions will be populated here -->
                <div class="loading">Loading sessions...</div>
            </div>
            
            <div class="picker-footer">
                <button id="refresh-sessions" class="btn-secondary">
                    🔄 Refresh
                </button>
            </div>
        </div>
        
        <!-- Chat Container (hidden initially) -->
        <div id="chat-container" class="chat-container" style="display: none;">
            <!-- Chat UI will be rendered here -->
        </div>
    </div>
    
    <!-- Service Worker Registration -->
    <script>
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/pwa/static/service-worker.js')
                    .then(registration => {
                        console.log('[PWA] Service Worker registered:', registration.scope);
                    })
                    .catch(error => {
                        console.error('[PWA] Service Worker registration failed:', error);
                    });
            });
        }
    </script>
    
    <!-- Main App Script (ES6 Module) -->
    <script type="module" src="/pwa/static/app.js"></script>
</body>
</html>
```

#### 2.3 Create `web/pwa/manifest.json`

```json
{
  "name": "Ren - ComfyUI Assistant",
  "short_name": "Ren",
  "description": "AI-powered ComfyUI workflow assistant",
  "start_url": "/pwa",
  "display": "standalone",
  "background_color": "#1a1420",
  "theme_color": "#2d1f3d",
  "orientation": "portrait",
  "icons": [
    {
      "src": "/pwa/static/icons/icon-192.png",
      "sizes": "192x192",
      "type": "image/png",
      "purpose": "any"
    },
    {
      "src": "/pwa/static/icons/icon-512.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "any"
    },
    {
      "src": "/pwa/static/icons/icon-maskable.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "maskable"
    }
  ],
  "categories": ["productivity", "utilities"],
  "screenshots": []
}
```

#### 2.4 Create `web/pwa/app.js`

```javascript
/**
 * Ren PWA - Main Application
 * 
 * Mobile Progressive Web App for controlling ComfyUI via Ren assistant.
 */

import SessionManager from '../js/session_manager.js';
import WSClient from '../js/ws_client.js';
import { ChatUI } from '../js/chat_ui.js';

class RenPWA {
    constructor() {
        this.sessionManager = null;
        this.wsClient = null;
        this.chatUI = null;
        this.currentSessionId = null;
        
        // Get backend URL from current location
        this.backendUrl = this.getBackendUrl();
        
        console.log('[RenPWA] Initializing...');
        console.log('[RenPWA] Backend URL:', this.backendUrl);
        
        this.init();
    }
    
    /**
     * Get backend URL based on current location
     */
    getBackendUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        return `${protocol}//${host}/ws`;
    }
    
    /**
     * Initialize PWA
     */
    async init() {
        // Show session picker
        await this.showSessionPicker();
        
        // Setup event listeners
        this.setupEventListeners();
    }
    
    /**
     * Setup event listeners
     */
    setupEventListeners() {
        // Refresh sessions button
        const refreshBtn = document.getElementById('refresh-sessions');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => this.loadSessions());
        }
    }
    
    /**
     * Show session picker and load available sessions
     */
    async showSessionPicker() {
        const picker = document.getElementById('session-picker');
        const chatContainer = document.getElementById('chat-container');
        
        picker.style.display = 'flex';
        chatContainer.style.display = 'none';
        
        await this.loadSessions();
    }
    
    /**
     * Load available sessions from backend
     */
    async loadSessions() {
        const sessionList = document.getElementById('session-list');
        sessionList.innerHTML = '<div class="loading">Loading sessions...</div>';
        
        try {
            const response = await fetch(`${window.location.origin}/api/sessions`);
            const data = await response.json();
            
            console.log('[RenPWA] Loaded sessions:', data);
            
            if (data.sessions.length === 0) {
                sessionList.innerHTML = `
                    <div class="no-sessions">
                        <p>🚫 No active sessions found</p>
                        <p class="hint">Open ComfyUI with FL_JS extension to create a session</p>
                    </div>
                `;
                return;
            }
            
            // Filter to show only sessions with frontend connection
            const activeSessions = data.sessions.filter(s => s.has_frontend);
            
            if (activeSessions.length === 0) {
                sessionList.innerHTML = `
                    <div class="no-sessions">
                        <p>🚫 No ComfyUI sessions found</p>
                        <p class="hint">Sessions exist but no ComfyUI frontend is connected</p>
                    </div>
                `;
                return;
            }
            
            // Render session list
            sessionList.innerHTML = activeSessions.map(session => `
                <div class="session-card" data-session-id="${session.session_id}">
                    <div class="session-info">
                        <div class="session-id">${session.session_id.substring(0, 8)}...</div>
                        <div class="session-status">
                            ${session.has_frontend ? '<span class="status-badge frontend">💻 ComfyUI</span>' : ''}
                            ${session.has_pwa ? '<span class="status-badge pwa">📱 Mobile</span>' : ''}
                        </div>
                        <div class="session-time">Last active: ${this.formatTime(session.last_activity)}</div>
                    </div>
                    <button class="btn-connect" data-session-id="${session.session_id}">
                        Connect →
                    </button>
                </div>
            `).join('');
            
            // Add click handlers to connect buttons
            sessionList.querySelectorAll('.btn-connect').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const sessionId = e.target.dataset.sessionId;
                    this.connectToSession(sessionId);
                });
            });
            
        } catch (error) {
            console.error('[RenPWA] Failed to load sessions:', error);
            sessionList.innerHTML = `
                <div class="error">
                    <p>❌ Failed to load sessions</p>
                    <p class="hint">${error.message}</p>
                </div>
            `;
        }
    }
    
    /**
     * Connect to a specific session
     */
    async connectToSession(sessionId) {
        console.log('[RenPWA] Connecting to session:', sessionId);
        
        this.currentSessionId = sessionId;
        
        // Hide session picker, show chat
        const picker = document.getElementById('session-picker');
        const chatContainer = document.getElementById('chat-container');
        
        picker.style.display = 'none';
        chatContainer.style.display = 'flex';
        
        // Initialize WebSocket client with PWA identifier
        this.wsClient = new WSClient(sessionId, {
            url: this.backendUrl,
            clientVersion: '1.0.0-pwa',  // Identifies as PWA connection
            maxReconnectAttempts: 10,
        });
        
        // Initialize Chat UI
        this.chatUI = new ChatUI(chatContainer, this.wsClient);
        
        // Setup WebSocket event handlers
        this.setupWebSocketHandlers();
        
        // Connect
        this.wsClient.connect();
        
        console.log('[RenPWA] Chat UI initialized');
    }
    
    /**
     * Setup WebSocket event handlers
     */
    setupWebSocketHandlers() {
        this.wsClient.on('connected', () => {
            console.log('[RenPWA] Connected to backend');
        });
        
        this.wsClient.on('disconnected', () => {
            console.log('[RenPWA] Disconnected from backend');
            // Could show reconnection UI here
        });
        
        this.wsClient.on('handshake_ack', (message) => {
            console.log('[RenPWA] Handshake complete:', message.status);
        });
        
        this.wsClient.on('error', (error) => {
            console.error('[RenPWA] Error:', error);
        });
        
        this.wsClient.on('max_reconnect_reached', () => {
            console.error('[RenPWA] Max reconnection attempts reached');
            // Show error UI and option to return to session picker
            if (confirm('Connection lost. Return to session picker?')) {
                this.showSessionPicker();
            }
        });
    }
    
    /**
     * Format ISO timestamp to relative time
     */
    formatTime(isoString) {
        const date = new Date(isoString);
        const now = new Date();
        const seconds = Math.floor((now - date) / 1000);
        
        if (seconds < 60) return 'just now';
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
        return `${Math.floor(seconds / 86400)}d ago`;
    }
}

// Initialize PWA when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        window.renPWA = new RenPWA();
    });
} else {
    window.renPWA = new RenPWA();
}

console.log('[RenPWA] Module loaded');
```

#### 2.5 Create `web/pwa/styles.css`

```css
/* Import base styles from chat UI */
@import url('../js/style.css');

/* PWA-specific overrides and additions */

* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

html, body {
    width: 100%;
    height: 100%;
    overflow: hidden;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(180deg, #1f1825 0%, #1a1420 100%);
    color: #E6D5E6;
}

#app {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
}

/* Session Picker Styles */
.session-picker {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    padding: 20px;
    overflow-y: auto;
}

.picker-header {
    text-align: center;
    margin-bottom: 24px;
    padding: 20px 0;
}

.picker-header h1 {
    font-size: 32px;
    font-weight: 700;
    color: #C8A2C8;
    margin-bottom: 8px;
}

.picker-header p {
    font-size: 14px;
    color: rgba(230, 213, 230, 0.7);
}

.session-list {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 20px;
}

.session-card {
    background: rgba(200, 162, 200, 0.08);
    border: 1px solid rgba(200, 162, 200, 0.2);
    border-radius: 12px;
    padding: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    transition: all 0.2s;
}

.session-card:active {
    background: rgba(200, 162, 200, 0.12);
    transform: scale(0.98);
}

.session-info {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.session-id {
    font-family: 'Courier New', monospace;
    font-size: 14px;
    font-weight: 600;
    color: #E6D5E6;
}

.session-status {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}

.status-badge {
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 6px;
    background: rgba(183, 148, 244, 0.15);
    border: 1px solid rgba(183, 148, 244, 0.3);
}

.status-badge.frontend {
    background: rgba(100, 181, 246, 0.15);
    border-color: rgba(100, 181, 246, 0.3);
}

.status-badge.pwa {
    background: rgba(200, 162, 200, 0.15);
    border-color: rgba(200, 162, 200, 0.3);
}

.session-time {
    font-size: 12px;
    color: rgba(230, 213, 230, 0.5);
}

.btn-connect {
    background: linear-gradient(135deg, #C8A2C8 0%, #B794F4 100%);
    border: none;
    border-radius: 10px;
    padding: 12px 24px;
    color: #2d1f3d;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: all 0.2s;
    box-shadow: 0 4px 12px rgba(200, 162, 200, 0.2);
    white-space: nowrap;
}

.btn-connect:active {
    transform: scale(0.95);
}

.picker-footer {
    display: flex;
    justify-content: center;
    padding: 12px 0;
}

.btn-secondary {
    background: rgba(200, 162, 200, 0.15);
    border: 1px solid rgba(200, 162, 200, 0.3);
    border-radius: 10px;
    padding: 12px 24px;
    color: #E6D5E6;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: all 0.2s;
}

.btn-secondary:active {
    background: rgba(200, 162, 200, 0.25);
    transform: scale(0.95);
}

.loading, .no-sessions, .error {
    text-align: center;
    padding: 40px 20px;
    color: rgba(230, 213, 230, 0.6);
}

.loading {
    font-size: 14px;
}

.no-sessions p:first-child,
.error p:first-child {
    font-size: 48px;
    margin-bottom: 12px;
}

.hint {
    font-size: 12px;
    color: rgba(230, 213, 230, 0.4);
    margin-top: 8px;
}

/* Chat Container */
.chat-container {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
}

/* Mobile optimizations */
@media (max-width: 768px) {
    .fl-chat-input {
        font-size: 16px; /* Prevents zoom on iOS */
    }
    
    .fl-message {
        max-width: 90%;
    }
    
    .fl-chat-send {
        padding: 0 16px;
    }
}

/* Prevent pull-to-refresh on mobile */
body {
    overscroll-behavior-y: contain;
}

/* Safe area insets for notched devices */
@supports (padding: max(0px)) {
    .session-picker,
    .chat-container {
        padding-left: max(20px, env(safe-area-inset-left));
        padding-right: max(20px, env(safe-area-inset-right));
        padding-bottom: max(20px, env(safe-area-inset-bottom));
    }
}
```

#### 2.6 Create `web/pwa/service-worker.js`

```javascript
/**
 * Service Worker for Ren PWA
 * 
 * Provides offline support and caching for PWA assets.
 */

const CACHE_NAME = 'ren-pwa-v1';
const urlsToCache = [
    '/pwa',
    '/pwa/',
    '/pwa/static/app.js',
    '/pwa/static/styles.css',
    '/pwa/static/manifest.json',
    '/pwa/static/icons/icon-192.png',
    '/pwa/static/icons/icon-512.png',
    // Shared JS modules
    '/web/js/session_manager.js',
    '/web/js/ws_client.js',
    '/web/js/chat_ui.js',
    '/web/js/style.css',
    '/web/js/_components/MessageBubble.js',
];

// Install event - cache assets
self.addEventListener('install', event => {
    console.log('[ServiceWorker] Installing...');
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => {
                console.log('[ServiceWorker] Caching app shell');
                return cache.addAll(urlsToCache);
            })
            .then(() => self.skipWaiting())
    );
});

// Activate event - cleanup old caches
self.addEventListener('activate', event => {
    console.log('[ServiceWorker] Activating...');
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('[ServiceWorker] Removing old cache:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => self.clients.claim())
    );
});

// Fetch event - serve from cache, fallback to network
self.addEventListener('fetch', event => {
    // Skip WebSocket requests
    if (event.request.url.startsWith('ws://') || event.request.url.startsWith('wss://')) {
        return;
    }
    
    // Skip API requests (always fetch fresh)
    if (event.request.url.includes('/api/')) {
        event.respondWith(fetch(event.request));
        return;
    }
    
    // Network-first strategy for PWA assets
    event.respondWith(
        fetch(event.request)
            .then(response => {
                // Clone the response
                const responseToCache = response.clone();
                
                // Update cache
                caches.open(CACHE_NAME)
                    .then(cache => {
                        cache.put(event.request, responseToCache);
                    });
                
                return response;
            })
            .catch(() => {
                // If network fails, try cache
                return caches.match(event.request)
                    .then(response => {
                        if (response) {
                            return response;
                        }
                        // If not in cache either, return offline page
                        return new Response('Offline - Please check your connection', {
                            status: 503,
                            statusText: 'Service Unavailable',
                            headers: new Headers({
                                'Content-Type': 'text/plain'
                            })
                        });
                    });
            })
    );
});
```

#### 2.7 Create PWA Icons

You'll need to create icon files. For now, create placeholder icons:

**Temporary Solution**: Use an online tool like https://www.pwabuilder.com/imageGenerator to generate icons from a base image.

**Required icons**:
- `web/pwa/icons/icon-192.png` (192x192)
- `web/pwa/icons/icon-512.png` (512x512)
- `web/pwa/icons/icon-maskable.png` (512x512 with safe zone)

---

### Phase 3: Testing

#### 3.1 Test Backend

1. Start backend:
   ```bash
   cd backend
   python server.py
   ```

2. Test endpoints:
   ```bash
   # Health check
   curl http://localhost:8000/health
   
   # Sessions list
   curl http://localhost:8000/api/sessions
   
   # PWA page
   curl http://localhost:8000/pwa
   ```

#### 3.2 Test PWA on Desktop

1. Open ComfyUI with FL_JS extension
2. Open `http://localhost:8000/pwa` in browser
3. Should see session picker with your ComfyUI session
4. Click "Connect" and test chat

#### 3.3 Test PWA on Mobile (via ngrok)

1. Install ngrok: https://ngrok.com/download

2. Start ngrok tunnel:
   ```bash
   ngrok http 8000
   ```

3. Note the HTTPS URL (e.g., `https://abc123.ngrok.io`)

4. On mobile browser, navigate to:
   ```
   https://abc123.ngrok.io/pwa
   ```

5. Test session picker and chat

6. Test PWA installation:
   - On iOS: Share button → "Add to Home Screen"
   - On Android: Menu → "Install app" or "Add to Home screen"

#### 3.4 Test Multi-Client Functionality

1. Open ComfyUI with FL_JS sidebar
2. Open PWA on mobile (or another browser tab)
3. Both should connect to same session
4. Send message from PWA
5. Verify:
   - Message appears in both clients
   - Agent response appears in both clients
   - Tool execution happens on ComfyUI side
   - Tool results visible in both clients

---

### Phase 4: Deployment Considerations

#### 4.1 Production Backend

**Update `backend/config.py`** to support production URLs:

```python
# Add to Settings class
pwa_url: str = ""  # Empty for same-origin, or specify full URL
```

**Update CORS** in `backend/server.py` for production:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8188",  # ComfyUI
        "http://localhost:8000",  # Backend
        settings.pwa_url,  # PWA URL if different
    ] if settings.pwa_url else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

#### 4.2 HTTPS Requirement

PWAs require HTTPS in production. Options:

1. **ngrok** (easiest for testing)
2. **Cloudflare Tunnel** (free, persistent)
3. **Reverse proxy** (nginx + Let's Encrypt)
4. **Cloud deployment** (AWS, GCP, Azure)

#### 4.3 Service Worker Updates

When you update PWA code:

1. Update `CACHE_NAME` in `service-worker.js` (e.g., `ren-pwa-v2`)
2. Users will get update on next visit
3. Consider adding update notification UI

---

## Summary

### Files Created
- `web/pwa/index.html` - PWA entry point
- `web/pwa/manifest.json` - PWA manifest
- `web/pwa/app.js` - Main PWA logic
- `web/pwa/styles.css` - Mobile-optimized styles
- `web/pwa/service-worker.js` - Offline support
- `web/pwa/icons/` - PWA icons (3 files)

### Files Modified
- `backend/server.py` - Added static file serving, session API, routing updates

### Code Reused
- `web/js/session_manager.js` - 100% reused
- `web/js/ws_client.js` - 100% reused
- `web/js/chat_ui.js` - 100% reused
- `web/js/style.css` - 100% reused (with additions)
- `web/js/_components/` - 100% reused

### Testing Checklist
- [ ] Backend serves PWA at `/pwa`
- [ ] Sessions API returns active sessions
- [ ] PWA shows session picker
- [ ] PWA connects to selected session
- [ ] Chat UI renders correctly on mobile
- [ ] Messages send/receive work
- [ ] Tool execution routes to ComfyUI
- [ ] Both clients see same conversation
- [ ] PWA installable on mobile
- [ ] Service worker caches assets
- [ ] Works via ngrok on mobile device

### Next Steps
1. Follow implementation steps in order
2. Test each phase before moving to next
3. Create PWA icons
4. Test on actual mobile device
5. Consider adding features:
   - Session creation from PWA
   - Push notifications
   - Image preview/gallery
   - Voice input
   - QR code session joining
