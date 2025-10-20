# Deep Investigation: Tool Activity Visibility Issues

## Root Cause Analysis

### 1. DOM Positioning Conflict
**File:** `ComfyUI/custom_nodes/ComfyUI_FL-Agent/web/js/chat_ui.js` (Lines 552-557)
```css
.fl-tool-activity-overlay {
  position: absolute;
  bottom: 100%; /* Above input container */
  left: 0;
  right: 0;
  z-index: 100;
}
```
**Issue:** The overlay is positioned relative to `.fl-chat-input-container` which lacks explicit positioning, causing viewport miscalculations.

### 2. Viewport Overflow
**Evidence:**
- Test results show cards exist in DOM but aren't visible
- Parent container (`fl-chat-layout`) has implicit `overflow: hidden`
- Absolute positioning with `bottom: 100%` pushes content above visible area

### 3. Animation Timing
**File:** `ComfyUI/custom_nodes/ComfyUI_FL-Agent/web/js/tool_activity.js` (Lines 167-168)
```javascript
setTimeout(() => {
  this.hideTool(requestId); // Default 30s cleanup
}, this.autoCleanupMs);
```
**Observation:** Despite long timeout, cards disappear immediately due to viewport issues.

## Recommended Debugging Path

1. **Viewport Verification Test**
```javascript
const card = document.querySelector('.fl-tool-card');
const cardRect = card.getBoundingClientRect();
console.log('Card visibility:', 
  cardRect.top >= 0 && 
  cardRect.left >= 0 &&
  cardRect.bottom <= window.innerHeight &&
  cardRect.right <= window.innerWidth
);
```

2. **CSS Debugging Overrides**
```javascript
const style = document.createElement('style');
style.textContent = `
  .fl-tool-activity-overlay {
    border: 2px solid red !important;
    overflow: visible !important;
    bottom: 80px !important; /* Test fixed position */
  }
  .fl-tool-card {
    border: 2px solid lime !important;
  }
`;
document.head.appendChild(style);
```

3. **Layout Flow Analysis**
The rendering sequence:
```
ToolActivity.showTool() 
→ _createCard() 
→ _addCard() (appends to overlay) 
→ requestAnimationFrame (sets opacity/transform)
```

## Core Issue Summary
The cards render outside the visible viewport due to:
1. Absolute positioning with `bottom: 100%`
2. Constrained parent container height
3. Missing overflow handling in layout containers