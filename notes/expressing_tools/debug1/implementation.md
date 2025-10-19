# Implementation Plan: Fixing Tool Activity Visibility

## Core Issues Identified
1. **Viewport Containment Failure** (Cards render outside visible area)
2. **Positioning Miscalculation** (`bottom: 100%` pushes content up)
3. **Missing Layout Safeguards** (No overflow handling)

## Targeted File Modifications

### 1. `web/js/chat_ui.js` (CSS Overrides)
```javascript
// Add to ChatUI's _initializeUI() method:
const style = document.createElement('style');
style.textContent = `
  .fl-chat-layout {
    overflow: visible !important;
    min-height: 100px; /* Ensure space for tool cards */
  }
  .fl-tool-activity-overlay {
    bottom: 20px !important; /* Fixed position above input */
    max-height: 60vh; /* Prevent overflow */
  }
`;
document.head.appendChild(style);
```

### 2. `web/js/tool_activity.js` (Positioning Logic)
```javascript
// Modify _createOverlay() method:
_createOverlay() {
  this.overlay = document.createElement('div');
  this.overlay.className = 'fl-tool-activity-overlay';
  
  // Insert below chat content instead of above input
  this.chatContainer.querySelector('.fl-chat-content').appendChild(this.overlay);
}
```

### 3. `web/js/tool_activity.js` (Visibility Safeguards)
```javascript
// Add to showTool() after card creation:
requestAnimationFrame(() => {
  const rect = card.getBoundingClientRect();
  if (rect.top < 0 || rect.bottom > window.innerHeight) {
    console.warn('[ToolActivity] Card out of viewport:', rect);
    this._adjustCardPosition(card);
  }
});
```

## Implementation Sequence
1. **Apply CSS Overrides** (Immediate visibility fix)
2. **Reposition Overlay** (Structural correction)
3. **Add Viewport Checks** (Future-proofing)

## Verification Tests
1. **Visual Inspection**:
   - Cards should appear above input area
   - Should remain visible during tool execution

2. **Console Validation**:
```javascript
// Run after implementation:
window.FL_JS.chatUI.toolActivity.showTool('workflow_overview', 'test-1');
const card = document.querySelector('.fl-tool-card');
console.log('Card position:', card.getBoundingClientRect());
```

## Expected Outcomes
- ✔️ Cards remain within viewport boundaries
- ✔️ No more "invisible but present" cards
- ✔️ Clean console warnings if positioning issues occur