# PWA for Ren Mobile Chat - Original Idea

## Goal
Create a Progressive Web App (PWA) that allows interaction with Ren from a mobile phone, enabling control of ComfyUI workflows remotely.

## Use Case
1. ComfyUI running on local machine with Ren extension
2. Ren extension connects via WebSocket to FL_JS backend
3. User accesses PWA on mobile phone (via ngrok or similar)
4. PWA connects to same backend via WebSocket
5. User can chat with Ren and control ComfyUI from phone

## Key Features

### Session Discovery
- PWA should show a list of available sessions
- User can select which session to join
- Multiple clients can connect to the same session
- PWA identifies itself as a mobile client

### Architecture
- **Backend**: Serve PWA from `backend/server.py` (new endpoint)
- **Frontend**: PWA code in `web/pwa/`
- **Reuse**: Leverage existing components from `web/js/`
  - `ws_client.js` - WebSocket client
  - `chat_ui.js` - Chat interface
  - `session_manager.js` - Session management
  - `tool_executor.js` - Tool execution
  - `_components/` - UI components

### Mobile-Specific Considerations
- Responsive design for mobile screens
- Touch-friendly interface
- Offline capability (PWA features)
- Service worker for caching
- Web app manifest for installation

### Security
- Not a primary concern for initial version
- Will use ngrok or similar for remote access
- Focus on functionality first

## Technical Approach

### Backend Changes
1. Add endpoint to serve PWA HTML/JS/CSS
2. WebSocket already supports multiple connections per session
3. Connection type detection (frontend vs PWA)

### Frontend Structure
```
web/pwa/
├── index.html          # PWA entry point
├── manifest.json       # PWA manifest
├── service-worker.js   # Service worker for offline
├── app.js              # Main PWA app logic
└── style.css           # Mobile-optimized styles
```

### Code Reuse
- Import modules from `../js/`
- Use same WebSocket protocol
- Same chat UI components with mobile styling
- Same tool execution flow

## Implementation Strategy
1. Document current architecture (investigation.md)
2. Plan code modifications (implementation.md)
3. Implement PWA with minimal changes to existing code
4. Test mobile functionality

## Future Enhancements
- Push notifications for workflow completion
- Image preview/gallery
- Voice input for chat
- Session management (create/delete sessions)
- Multi-user collaboration features
