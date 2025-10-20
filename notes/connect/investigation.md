# Connect Tool Implementation Investigation

## Objective

Understand exactly how the current `connect_nodes` implementation works, what data structures are available, and what changes are needed to make connections reliable for AI agents.

---

## Current Implementation Deep Dive

### File: `web/js/fl_api.js` - `connect()` Method

**Location**: Lines 390-470

**Method Signature**:
```javascript
connect(sourceId, sourceSlot, targetId, targetSlot = null)
```

**Parameters**:
- `sourceId`: Node ID, title, or node object
- `sourceSlot`: Slot name (string) or index (number)
- `targetId`: Node ID, title, or node object  
- `targetSlot`: Slot name (string) or index (number), defaults to `sourceSlot` if null

**Returns**: `boolean` - True if connection successful

---

### Connection Logic Flow

#### Step 1: Find Nodes
```javascript
const sourceNode = this._findNode(sourceId);
const targetNode = this._findNode(targetId);

if (!sourceNode || !targetNode) {
    throw new Error("Source or target node not found");
}
```
✅ **Works correctly** - Uses existing `_findNode` helper

---

#### Step 2: Default Target Slot
```javascript
if (targetSlot === null) {
    targetSlot = sourceSlot;
}
```
⚠️ **Assumption**: If no target slot specified, use same name as source slot
- **Problem**: Output and input slots rarely have the same name
- **Example**: Output "LATENT" ≠ Input "latent_image"

---

#### Step 3: Case Conversion (PROBLEMATIC)
```javascript
const sourceSlotName = typeof sourceSlot === "string" ? sourceSlot.toUpperCase() : null;
const targetSlotName = typeof targetSlot === "string" ? targetSlot.toLowerCase() : null;
```

❌ **MAJOR FLAW**: Assumes:
- All output slots are uppercase
- All input slots are lowercase

**Reality Check** - ComfyUI slot naming:
- KSampler outputs: `"LATENT"` (uppercase) ✅
- VAEDecode inputs: `"samples"` (lowercase) ✅
- But also: `"latent_image"` (lowercase with underscore)
- And: Mixed case in custom nodes

**Why this exists**: Probably an attempt to make matching case-insensitive, but implemented incorrectly.

---

#### Step 4: Find Output Slot Index
```javascript
let outputSlotIndex;
if (typeof sourceSlot === "number") {
    outputSlotIndex = sourceSlot;
} else if (sourceSlotName && sourceNode.outputs) {
    const output = sourceNode.outputs.find(o => o.name.toUpperCase() === sourceSlotName);
    if (output) {
        outputSlotIndex = sourceNode.findOutputSlot(output.name);
    }
}
```

**Logic**:
1. If slot is a number, use it directly as index
2. Otherwise, search outputs array for matching name (case-insensitive via toUpperCase)
3. If found, get the index using `findOutputSlot()`

**Data Structure** - `sourceNode.outputs`:
```javascript
[
    { name: "LATENT", type: "LATENT", links: [] },
    { name: "IMAGE", type: "IMAGE", links: [] }
]
```

**LiteGraph Method** - `node.findOutputSlot(name)`:
- Returns the index of the output slot with the given name
- Case-sensitive
- Returns -1 if not found

---

#### Step 5: Find Input Slot Index
```javascript
let inputSlotIndex;
if (typeof targetSlot === "number") {
    inputSlotIndex = targetSlot;
} else if (targetSlotName && targetNode.inputs) {
    const input = targetNode.inputs.find(i => i.name.toLowerCase() === targetSlotName);
    if (input) {
        inputSlotIndex = targetNode.findInputSlot(input.name);
    }
}
```

**Same logic as Step 4** but for inputs.

**Data Structure** - `targetNode.inputs`:
```javascript
[
    { name: "model", type: "MODEL", link: null },
    { name: "positive", type: "CONDITIONING", link: null },
    { name: "negative", type: "CONDITIONING", link: null },
    { name: "latent_image", type: "LATENT", link: null }
]
```

---

#### Step 6: Auto-Matching Fallback
```javascript
if (outputSlotIndex === undefined && sourceNode.outputs) {
    const firstOutput = sourceNode.outputs[0];
    if (firstOutput) {
        outputSlotIndex = 0;
        // Try to find matching input by type
        if (inputSlotIndex === undefined && targetNode.inputs) {
            const matchingInput = targetNode.inputs.find(i => i.type === firstOutput.type);
            if (matchingInput) {
                inputSlotIndex = targetNode.findInputSlot(matchingInput.name);
            }
        }
    }
}
```

**Auto-Matching Logic**:
1. If output slot not found, use first output (index 0)
2. If input slot also not found, search for input with matching TYPE
3. This is a **type-based fallback**

⚠️ **Problem**: Only works if BOTH slots are undefined
- If agent provides wrong slot names, auto-match doesn't trigger
- Only triggers if agent provides NO slot names

---

#### Step 7: Execute Connection
```javascript
if (typeof outputSlotIndex === "number" && typeof inputSlotIndex === "number") {
    sourceNode.connect(outputSlotIndex, targetNode.id, inputSlotIndex);
    console.log(
        `[FL_API] Connected: ${sourceNode.id}[${outputSlotIndex}] -> ` +
        `${targetNode.id}[${inputSlotIndex}]`
    );
    return true;
}

throw new Error("Could not find matching slots for connection");
```

**LiteGraph Method** - `sourceNode.connect(outputIndex, targetNodeId, targetInputIndex)`:
- Native LiteGraph connection method
- Creates a link between the specified slots
- Returns the link object

❌ **Error Handling**: Generic error message with no details about:
- What slots were attempted
- What slots are available
- Why the match failed

---

## Why Connections Are Failing

### Failed Attempt 1 Analysis

**Agent Request**:
```javascript
connect_nodes({
    source_node_id: 3,
    source_slot: "LATENT",
    target_node_id: 5,
    target_slot: "latent_image"
})
```

**Execution Trace**:

1. **Find nodes**: ✅ Nodes 3 and 5 found

2. **Case conversion**:
   - `sourceSlotName = "LATENT".toUpperCase() = "LATENT"`
   - `targetSlotName = "latent_image".toLowerCase() = "latent_image"`

3. **Find output slot**:
   ```javascript
   sourceNode.outputs.find(o => o.name.toUpperCase() === "LATENT")
   // Looking for: o.name.toUpperCase() === "LATENT"
   // If output is {name: "LATENT", ...}: "LATENT" === "LATENT" ✅ MATCH
   ```
   ✅ **Output slot found** (assuming node 3 has "LATENT" output)

4. **Find input slot**:
   ```javascript
   targetNode.inputs.find(i => i.name.toLowerCase() === "latent_image")
   // Looking for: i.name.toLowerCase() === "latent_image"
   // If input is {name: "samples", ...}: "samples" === "latent_image" ❌ NO MATCH
   // If input is {name: "latent", ...}: "latent" === "latent_image" ❌ NO MATCH
   ```
   
   ❌ **Input slot NOT found** - Node 5 doesn't have input named "latent_image"
   - Actual input is probably named "latent" or "samples"

5. **Auto-match check**:
   ```javascript
   if (outputSlotIndex === undefined && ...) {
   // outputSlotIndex is 0 (found), so condition is FALSE
   ```
   ❌ **Auto-match doesn't trigger** because output was found

6. **Connection attempt**:
   ```javascript
   if (typeof outputSlotIndex === "number" && typeof inputSlotIndex === "number") {
   // outputSlotIndex = 0 (number)
   // inputSlotIndex = undefined (not a number)
   // Condition is FALSE
   ```
   
7. **Result**: Throws error "Could not find matching slots for connection"

---

### Root Cause

The agent provided:
- ✅ Correct source slot: `"LATENT"`
- ❌ Incorrect target slot: `"latent_image"` (actual slot is probably `"latent"` or `"samples"`)

**Why did agent guess wrong?**
- Agent has no way to discover actual slot names
- Agent guessed based on general ComfyUI knowledge
- Different nodes use different naming conventions

---

## Available Data Structures

### 1. LiteGraph Node Object

**Structure** (from ComfyUI/LiteGraph):
```javascript
{
    id: 3,
    type: "KSampler",
    title: "KSampler",
    comfyClass: "KSampler",
    pos: [100, 200],
    size: [300, 400],
    mode: 0,  // 0=normal, 2=muted, 4=bypassed
    
    // Widget parameters
    widgets: [
        { name: "seed", type: "number", value: 12345 },
        { name: "steps", type: "number", value: 20 },
        ...
    ],
    
    // Input slots
    inputs: [
        {
            name: "model",
            type: "MODEL",
            link: 1  // Link ID or null
        },
        {
            name: "positive",
            type: "CONDITIONING",
            link: 2
        },
        {
            name: "latent_image",
            type: "LATENT",
            link: null
        }
    ],
    
    // Output slots
    outputs: [
        {
            name: "LATENT",
            type: "LATENT",
            links: []  // Array of link IDs
        }
    ],
    
    // Graph reference
    graph: <LGraph>,
    
    // Methods
    findInputSlot(name),   // Returns index or -1
    findOutputSlot(name),  // Returns index or -1
    connect(outputIndex, targetNodeId, targetInputIndex)
}
```

---

### 2. Query Executor Serialized Node

**From** `web/js/query_executor.js` - `serializeNode()` method:

```javascript
{
    id: 3,
    type: "KSampler",
    title: "KSampler",
    position: { x: 100, y: 200 },
    size: { width: 300, height: 400 },
    mode: 0,
    
    parameters: {
        seed: 12345,
        steps: 20,
        cfg: 7.0,
        ...
    },
    
    connections: {
        inputs: [
            {
                slot: "model",
                type: "MODEL",
                connected_to: [
                    { node_id: 2, slot: 0 }
                ]
            },
            {
                slot: "latent_image",
                type: "LATENT",
                connected_to: []  // Not connected
            }
        ],
        outputs: [
            {
                slot: "LATENT",
                type: "LATENT",
                connected_to: []  // Not connected
            }
        ]
    }
}
```

✅ **This structure has all slot information!**

---

### 3. get_current_user_focus Response

**From** `web/js/fl_api.js` - `getSelectedNodes()` method (just added):

```javascript
{
    nodes: [
        {
            id: 3,
            title: "KSampler",
            type: "KSampler",
            position: { x: 100, y: 200 },
            size: { width: 300, height: 400 },
            mode: 0,
            
            parameters: {
                seed: 12345,
                steps: 20,
                ...
            },
            
            inputs: [
                {
                    name: "model",
                    type: "MODEL",
                    link: 1
                },
                {
                    name: "latent_image",
                    type: "LATENT",
                    link: null
                }
            ],
            
            outputs: [
                {
                    name: "LATENT",
                    type: "LATENT",
                    links: []
                }
            ]
        }
    ]
}
```

✅ **Also has slot information!**

---

## What Information Is Available

### Via `query_workflow` Tool

✅ **Available**:
- All nodes in workflow
- Full slot names and types
- Current connections
- Connection graph structure

❌ **Problems**:
- Returns ALL nodes (can be overwhelming)
- Requires complex query DSL
- Agent must parse through many nodes

---

### Via `get_current_user_focus` Tool

✅ **Available**:
- Selected nodes only
- Full slot names and types  
- Current connections
- Parameters

❌ **Problems**:
- Only works if user selected the nodes
- Requires user interaction
- Not programmatic

---

### Via `find_node` Tool

❌ **NOT Available**:
- Slot information
- Only returns basic metadata

---

## Missing Tool: `get_node_slots`

### What Agent Needs

A tool to get slot information for a specific node by ID:

```python
@mcp.tool()
async def get_node_slots(node_id: int) -> dict:
    """Get input/output slot information for a specific node."""
```

**Response Structure**:
```json
{
    "node_id": 3,
    "type": "KSampler",
    "title": "KSampler",
    "inputs": [
        {
            "name": "model",
            "type": "MODEL",
            "index": 0,
            "connected": true,
            "connected_from": {"node_id": 2, "slot_name": "MODEL", "slot_index": 0}
        },
        {
            "name": "latent_image",
            "type": "LATENT",
            "index": 3,
            "connected": false
        }
    ],
    "outputs": [
        {
            "name": "LATENT",
            "type": "LATENT",
            "index": 0,
            "connected": false,
            "connected_to": []
        }
    ]
}
```

---

## Implementation Plan for `get_node_slots`

### Frontend: `web/js/fl_api.js`

**Add new method**:
```javascript
/**
 * Get slot information for a node
 * @param {number|string|object} nodeId - Node ID, title, or object
 * @returns {object} Slot information
 */
getNodeSlots(nodeId) {
    try {
        const node = this._findNode(nodeId);
        if (!node) {
            throw new Error(`Node not found: ${nodeId}`);
        }
        
        const inputs = [];
        if (node.inputs) {
            for (let i = 0; i < node.inputs.length; i++) {
                const input = node.inputs[i];
                const slotInfo = {
                    name: input.name,
                    type: input.type,
                    index: i,
                    connected: input.link !== null && input.link !== undefined
                };
                
                // Add connection details if connected
                if (slotInfo.connected) {
                    const link = node.graph.links[input.link];
                    if (link) {
                        slotInfo.connected_from = {
                            node_id: link.origin_id,
                            slot_name: link.origin_slot,  // May be undefined
                            slot_index: link.origin_slot
                        };
                    }
                }
                
                inputs.push(slotInfo);
            }
        }
        
        const outputs = [];
        if (node.outputs) {
            for (let i = 0; i < node.outputs.length; i++) {
                const output = node.outputs[i];
                const slotInfo = {
                    name: output.name,
                    type: output.type,
                    index: i,
                    connected: output.links && output.links.length > 0,
                    connected_to: []
                };
                
                // Add connection details if connected
                if (slotInfo.connected) {
                    for (const linkId of output.links) {
                        const link = node.graph.links[linkId];
                        if (link) {
                            slotInfo.connected_to.push({
                                node_id: link.target_id,
                                slot_name: link.target_slot,  // May be undefined
                                slot_index: link.target_slot
                            });
                        }
                    }
                }
                
                outputs.push(slotInfo);
            }
        }
        
        console.log(`[FL_API] Retrieved slots for node ${node.id}`);
        return {
            node_id: node.id,
            type: node.comfyClass || node.type,
            title: node.title,
            inputs,
            outputs
        };
    } catch (error) {
        console.error("[FL_API] getNodeSlots error:", error);
        throw error;
    }
}
```

---

### Frontend: `web/js/tool_executor.js`

**Add handler registration**:
```javascript
"get_node_slots": this._handleGetNodeSlots.bind(this),
```

**Add handler implementation**:
```javascript
async _handleGetNodeSlots(params) {
    const { node_id } = params;
    return this.flApi.getNodeSlots(node_id);
}
```

---

### Backend: `backend/mcp_server.py`

**Add request model**:
```python
class GetNodeSlotsRequest(BaseModel):
    """Request to get node slot information."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
```

**Add tool definition**:
```python
@mcp.tool()
async def get_node_slots(request: GetNodeSlotsRequest, ctx: Context) -> Dict[str, Any]:
    """Get input and output slot information for a node.
    
    This tool provides detailed information about a node's connection points,
    enabling agents to discover exact slot names and types before attempting
    to connect nodes.
    
    USE CASES:
    - Before connecting nodes: Discover available slots and their names
    - Check connection status: See which slots are already connected
    - Type matching: Find compatible slots by type
    - Debugging: Understand why connections fail
    
    RETURNS:
    Dictionary with:
    - node_id: Node ID (integer)
    - type: Node type/class (string)
    - title: Node title (string)
    - inputs: Array of input slot objects
    - outputs: Array of output slot objects
    
    Each slot object includes:
    - name: Exact slot name (string, case-sensitive)
    - type: Data type (e.g., "LATENT", "IMAGE", "MODEL")
    - index: Slot index (integer, for direct connection)
    - connected: Whether slot is currently connected (boolean)
    - connected_from/connected_to: Connection details if connected
    
    AGENT WORKFLOW:
    1. Agent wants to connect node A to node B
    2. Call get_node_slots(A) to get output slots
    3. Call get_node_slots(B) to get input slots
    4. Match slots by type compatibility
    5. Call connect_nodes with exact slot names
    6. Success!
    
    EXAMPLE:
    Agent: "Connect KSampler to VAEDecode"
    1. get_node_slots(3) → outputs: [{name: "LATENT", type: "LATENT"}]
    2. get_node_slots(5) → inputs: [{name: "samples", type: "LATENT"}]
    3. Match by type: "LATENT" → "LATENT"
    4. connect_nodes(3, "LATENT", 5, "samples")
    5. ✅ Success!
    """
    return await _execute_tool(ctx, "get_node_slots", request.model_dump())
```

---

## Improved `connect_nodes` Error Handling

### Current Error
```javascript
throw new Error("Could not find matching slots for connection");
```

### Improved Error
```javascript
// Build detailed error message
const availableOutputs = sourceNode.outputs ? 
    sourceNode.outputs.map(o => `"${o.name}" (${o.type})`).join(", ") : "none";
const availableInputs = targetNode.inputs ?
    targetNode.inputs.map(i => `"${i.name}" (${i.type})`).join(", ") : "none";

throw new Error(
    `Could not find matching slots for connection.\n` +
    `Attempted: "${sourceSlot}" → "${targetSlot}"\n` +
    `Available outputs on node ${sourceNode.id}: ${availableOutputs}\n` +
    `Available inputs on node ${targetNode.id}: ${availableInputs}`
);
```

**Example Error**:
```
Could not find matching slots for connection.
Attempted: "LATENT" → "latent_image"
Available outputs on node 3: "LATENT" (LATENT)
Available inputs on node 5: "samples" (LATENT), "vae" (VAE)
```

✅ **Now agent can learn** what the actual slot names are!

---

## Case-Insensitive Matching Fix

### Current (Broken)
```javascript
const sourceSlotName = typeof sourceSlot === "string" ? sourceSlot.toUpperCase() : null;
const targetSlotName = typeof targetSlot === "string" ? targetSlot.toLowerCase() : null;
```

### Fixed (Case-Insensitive Comparison)
```javascript
// Don't convert to upper/lower, just normalize for comparison
const normalizeSlotName = (name) => name.toLowerCase().trim();

// Find output slot (case-insensitive)
if (typeof sourceSlot === "string" && sourceNode.outputs) {
    const normalizedSource = normalizeSlotName(sourceSlot);
    const output = sourceNode.outputs.find(o => 
        normalizeSlotName(o.name) === normalizedSource
    );
    if (output) {
        outputSlotIndex = sourceNode.findOutputSlot(output.name);
    }
}

// Find input slot (case-insensitive)
if (typeof targetSlot === "string" && targetNode.inputs) {
    const normalizedTarget = normalizeSlotName(targetSlot);
    const input = targetNode.inputs.find(i => 
        normalizeSlotName(i.name) === normalizedTarget
    );
    if (input) {
        inputSlotIndex = targetNode.findInputSlot(input.name);
    }
}
```

✅ **Now works for any casing**: "LATENT", "latent", "Latent" all match

---

## Summary of Findings

### Problems Identified

1. ❌ **No slot discovery tool** - Agent can't find out what slots exist
2. ❌ **Case-sensitivity assumptions** - Broken uppercase/lowercase logic
3. ❌ **Poor error messages** - Agent can't learn from failures
4. ❌ **Auto-match doesn't help** - Only triggers if NO slots provided
5. ❌ **No type-based matching** - Agent must know exact names

### Solutions

1. ✅ **Add `get_node_slots` tool** - Enable slot discovery
2. ✅ **Fix case-insensitive matching** - Normalize both sides
3. ✅ **Improve error messages** - Show available slots
4. ✅ **Enhance auto-matching** - Trigger even when slots provided but wrong
5. ✅ **Add type-based connection** - Optional auto-match by type

---

## Next Steps

1. ✅ Analysis complete
2. ✅ Investigation complete  
3. ⏭️ Create `proposal.md` with detailed implementation plan
4. ⏭️ Implement Phase 1: `get_node_slots` + error improvements
5. ⏭️ Implement Phase 2: Enhanced `connect_nodes` with auto-matching
6. ⏭️ Implement Phase 3: Batch connection tools
