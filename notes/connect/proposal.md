# Proposal: Agent-Optimized Node Connection System

## Executive Summary

The current `connect_nodes` tool has a **33% success rate** due to agents lacking visibility into slot names and relying on guesswork. This proposal outlines a **3-phase implementation** to achieve **>95% success rate** and enable efficient batch connections.

**Key Changes**:
1. **Phase 1**: Add slot discovery tool + fix errors (2-3 hours)
2. **Phase 2**: Enhanced smart connection (2-3 hours)
3. **Phase 3**: Batch and auto-connect tools (3-4 hours)

**Total Effort**: 7-10 hours of development

---

## Problem Recap

### Current Failure Pattern

```
Agent: connect_nodes(source_id=3, source_slot="LATENT", 
                     target_id=5, target_slot="latent_image")

Backend: ❌ Error: "Could not find matching slots for connection"

Agent: 🤷 *has no idea what the actual slot names are*
```

### Root Causes

1. **Information Gap**: No tool to discover slot names
2. **Broken Matching**: Case-sensitivity assumptions fail
3. **Poor Feedback**: Generic errors don't help learning
4. **Manual Process**: Every connection requires perfect knowledge

---

## Phase 1: Slot Discovery & Error Improvements

### Goal
Enable agents to discover exact slot names and understand connection failures.

### Success Metrics
- Connection success rate: **>95%** (up from 33%)
- Error messages provide actionable information
- Agent can learn from failures

---

### 1.1: Add `get_node_slots` Tool

#### MCP Tool Definition

**File**: `backend/mcp_server.py`

```python
class GetNodeSlotsRequest(BaseModel):
    """Request to get node slot information."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

@mcp.tool()
async def get_node_slots(request: GetNodeSlotsRequest, ctx: Context) -> Dict[str, Any]:
    """Get detailed input/output slot information for a node.
    
    This tool enables agents to discover exact slot names and types before
    attempting connections, eliminating guesswork and connection failures.
    
    USE CASES:
    - Pre-connection discovery: "What slots does this node have?"
    - Type matching: "Which output type matches this input?"
    - Connection debugging: "Why did my connection fail?"
    - Workflow planning: "Can I connect these nodes?"
    
    RETURNS:
    {
        "node_id": 3,
        "type": "KSampler",
        "title": "KSampler",
        "inputs": [
            {
                "name": "latent_image",  // Exact slot name (case-sensitive)
                "type": "LATENT",         // Data type
                "index": 3,               // Slot index for direct connection
                "connected": false,       // Connection status
                "required": true          // Whether slot must be connected
            }
        ],
        "outputs": [
            {
                "name": "LATENT",
                "type": "LATENT",
                "index": 0,
                "connected": false,
                "connected_to": []       // Array of {node_id, slot_name, slot_index}
            }
        ]
    }
    
    AGENT WORKFLOW:
    1. Agent wants to connect node A → node B
    2. get_node_slots(A) → Discover output slots
    3. get_node_slots(B) → Discover input slots  
    4. Match by type: Find compatible LATENT → LATENT
    5. connect_nodes(A, "LATENT", B, "samples") with exact names
    6. ✅ Success!
    
    EXAMPLE:
    ```
    # Discover KSampler outputs
    result = get_node_slots(node_id=3)
    # Returns: outputs=[{name: "LATENT", type: "LATENT", ...}]
    
    # Discover VAEDecode inputs  
    result = get_node_slots(node_id=5)
    # Returns: inputs=[{name: "samples", type: "LATENT", ...}]
    
    # Now connect with exact names
    connect_nodes(3, "LATENT", 5, "samples")
    ```
    """
    return await _execute_tool(ctx, "get_node_slots", request.model_dump())
```

---

#### Frontend Implementation

**File**: `web/js/fl_api.js`

**Add method after `getSelectedNodes()` (around line 330)**:

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
                    connected: input.link !== null && input.link !== undefined,
                    required: true  // ComfyUI doesn't expose this, assume true
                };
                
                // Add connection details if connected
                if (slotInfo.connected && node.graph.links[input.link]) {
                    const link = node.graph.links[input.link];
                    slotInfo.connected_from = {
                        node_id: link.origin_id,
                        slot_index: link.origin_slot
                    };
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

**File**: `web/js/tool_executor.js`

**Add handler registration** (line ~51):
```javascript
"get_node_slots": this._handleGetNodeSlots.bind(this),
```

**Add handler implementation** (after `_handleGetSelectedNodes`, line ~313):
```javascript
async _handleGetNodeSlots(params) {
    const { node_id } = params;
    return this.flApi.getNodeSlots(node_id);
}
```

---

### 1.2: Fix Case-Insensitive Matching

**File**: `web/js/fl_api.js`

**Replace lines 414-435** with:

```javascript
// Helper for case-insensitive slot name comparison
const normalizeSlotName = (name) => String(name).toLowerCase().trim();

// Find output slot
let outputSlotIndex;
if (typeof sourceSlot === "number") {
    outputSlotIndex = sourceSlot;
} else if (typeof sourceSlot === "string" && sourceNode.outputs) {
    const normalizedSource = normalizeSlotName(sourceSlot);
    const output = sourceNode.outputs.find(o => 
        normalizeSlotName(o.name) === normalizedSource
    );
    if (output) {
        outputSlotIndex = sourceNode.findOutputSlot(output.name);
    }
}

// Find input slot
let inputSlotIndex;
if (typeof targetSlot === "number") {
    inputSlotIndex = targetSlot;
} else if (typeof targetSlot === "string" && targetNode.inputs) {
    const normalizedTarget = normalizeSlotName(targetSlot);
    const input = targetNode.inputs.find(i => 
        normalizeSlotName(i.name) === normalizedTarget
    );
    if (input) {
        inputSlotIndex = targetNode.findInputSlot(input.name);
    }
}
```

**Why This Works**:
- Normalizes BOTH sides to lowercase before comparison
- Preserves original name for `findInputSlot`/`findOutputSlot` (which are case-sensitive)
- Handles "LATENT", "latent", "Latent" all matching

---

### 1.3: Improve Error Messages

**File**: `web/js/fl_api.js`

**Replace line 463** (the error throw) with:

```javascript
// Build detailed error message with available slots
const availableOutputs = sourceNode.outputs ? 
    sourceNode.outputs.map(o => `"${o.name}" (${o.type})`).join(", ") : "none";
const availableInputs = targetNode.inputs ?
    targetNode.inputs.map(i => `"${i.name}" (${i.type})`).join(", ") : "none";

const errorMsg = [
    `Could not find matching slots for connection.`,
    `Attempted: source="${sourceSlot}" → target="${targetSlot}"`,
    `Source node ${sourceNode.id} (${sourceNode.type}) outputs: ${availableOutputs}`,
    `Target node ${targetNode.id} (${targetNode.type}) inputs: ${availableInputs}`,
    ``,
    `TIP: Use get_node_slots(node_id) to discover exact slot names.`
].join("\n");

throw new Error(errorMsg);
```

**Example Error Output**:
```
Could not find matching slots for connection.
Attempted: source="LATENT" → target="latent_image"
Source node 3 (KSampler) outputs: "LATENT" (LATENT)
Target node 5 (VAEDecode) inputs: "samples" (LATENT), "vae" (VAE)

TIP: Use get_node_slots(node_id) to discover exact slot names.
```

✅ **Agent can now learn** the correct slot name is "samples", not "latent_image"!

---

## Phase 2: Smart Connection Enhancements

### Goal
Reduce agent effort by adding intelligent auto-matching and optional parameters.

### Success Metrics
- Agent can connect nodes with just IDs (no slot names needed)
- Type-based matching works reliably
- Backwards compatible with existing code

---

### 2.1: Enhanced `connect_nodes` with Auto-Matching

#### Updated MCP Tool Definition

**File**: `backend/mcp_server.py`

**Modify `ConnectNodesRequest`**:
```python
class ConnectNodesRequest(BaseModel):
    """Request to connect two nodes."""
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title")
    
    # Make slots optional for auto-matching
    source_slot: Optional[Union[str, int]] = Field(
        None, 
        description="Source output slot name or index (auto-match if not provided)"
    )
    target_slot: Optional[Union[str, int]] = Field(
        None, 
        description="Target input slot name or index (auto-match if not provided)"
    )
    
    # New: Auto-match strategy
    auto_match: bool = Field(
        True,
        description="Enable auto-matching by type if slot names not found"
    )
    match_strategy: Literal["first", "type", "name"] = Field(
        "type",
        description="Auto-match strategy: 'first'=first compatible, 'type'=by type, 'name'=by similar name"
    )
```

**Update tool docstring**:
```python
@mcp.tool()
async def connect_nodes(request: ConnectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Connect two nodes with optional auto-matching.
    
    BASIC USAGE (with slot names):
    connect_nodes(source_node_id=3, source_slot="LATENT", 
                  target_node_id=5, target_slot="samples")
    
    SMART USAGE (auto-match by type):
    connect_nodes(source_node_id=3, target_node_id=5)
    # Automatically finds compatible LATENT → LATENT connection
    
    PARAMETERS:
    - source_node_id: Source node ID or title (required)
    - target_node_id: Target node ID or title (required)
    - source_slot: Output slot name/index (optional, auto-matches if not provided)
    - target_slot: Input slot name/index (optional, auto-matches if not provided)
    - auto_match: Enable auto-matching (default: true)
    - match_strategy: How to auto-match (default: "type")
      - "first": Use first available output/input
      - "type": Match by compatible types
      - "name": Match by similar slot names
    
    RETURNS:
    {
        "connected": true,
        "connection": {
            "source_node_id": 3,
            "source_slot": "LATENT",
            "source_slot_index": 0,
            "target_node_id": 5,
            "target_slot": "samples",
            "target_slot_index": 0,
            "type": "LATENT"
        }
    }
    
    ERROR HANDLING:
    If connection fails, error message includes:
    - What was attempted
    - Available slots on both nodes
    - Suggestion to use get_node_slots() for discovery
    
    AGENT WORKFLOW OPTIONS:
    
    Option 1 - Explicit (safest):
    1. get_node_slots(source_id) → Discover outputs
    2. get_node_slots(target_id) → Discover inputs
    3. connect_nodes(source_id, "exact_name", target_id, "exact_name")
    
    Option 2 - Smart (recommended):
    1. connect_nodes(source_id, target_id)  # Auto-match by type
    2. If fails, fall back to Option 1
    
    Option 3 - Semi-explicit:
    1. connect_nodes(source_id, "LATENT", target_id)  # Auto-match input
    """
    return await _execute_tool(ctx, "connect_nodes", request.model_dump())
```

---

#### Frontend Implementation

**File**: `web/js/fl_api.js`

**Replace the `connect()` method** (lines 390-470) with enhanced version:

```javascript
/**
 * Connect two nodes with optional auto-matching
 * @param {number|string|object} sourceId - Source node
 * @param {string|number|null} sourceSlot - Source slot name/index (null for auto)
 * @param {number|string|object} targetId - Target node
 * @param {string|number|null} targetSlot - Target slot name/index (null for auto)
 * @param {object} options - Connection options
 * @returns {object} Connection details
 */
connect(sourceId, sourceSlot = null, targetId, targetSlot = null, options = {}) {
    try {
        const sourceNode = this._findNode(sourceId);
        const targetNode = this._findNode(targetId);

        if (!sourceNode || !targetNode) {
            throw new Error("Source or target node not found");
        }

        // Options
        const autoMatch = options.auto_match !== false;  // Default true
        const matchStrategy = options.match_strategy || "type";  // Default "type"

        // Helper for case-insensitive slot name comparison
        const normalizeSlotName = (name) => String(name).toLowerCase().trim();

        // Find output slot
        let outputSlotIndex;
        let outputSlotName;
        
        if (typeof sourceSlot === "number") {
            // Direct index provided
            outputSlotIndex = sourceSlot;
            outputSlotName = sourceNode.outputs[sourceSlot]?.name;
        } else if (typeof sourceSlot === "string" && sourceNode.outputs) {
            // Slot name provided - find by name (case-insensitive)
            const normalizedSource = normalizeSlotName(sourceSlot);
            const output = sourceNode.outputs.find(o => 
                normalizeSlotName(o.name) === normalizedSource
            );
            if (output) {
                outputSlotIndex = sourceNode.findOutputSlot(output.name);
                outputSlotName = output.name;
            }
        }

        // Find input slot
        let inputSlotIndex;
        let inputSlotName;
        
        if (typeof targetSlot === "number") {
            // Direct index provided
            inputSlotIndex = targetSlot;
            inputSlotName = targetNode.inputs[targetSlot]?.name;
        } else if (typeof targetSlot === "string" && targetNode.inputs) {
            // Slot name provided - find by name (case-insensitive)
            const normalizedTarget = normalizeSlotName(targetSlot);
            const input = targetNode.inputs.find(i => 
                normalizeSlotName(i.name) === normalizedTarget
            );
            if (input) {
                inputSlotIndex = targetNode.findInputSlot(input.name);
                inputSlotName = input.name;
            }
        }

        // Auto-matching if enabled and slots not found
        if (autoMatch) {
            if (outputSlotIndex === undefined && sourceNode.outputs && sourceNode.outputs.length > 0) {
                if (matchStrategy === "first") {
                    // Use first output
                    outputSlotIndex = 0;
                    outputSlotName = sourceNode.outputs[0].name;
                } else if (matchStrategy === "type" && inputSlotIndex !== undefined) {
                    // Match by type if we know the input type
                    const inputType = targetNode.inputs[inputSlotIndex]?.type;
                    const matchingOutput = sourceNode.outputs.find(o => o.type === inputType);
                    if (matchingOutput) {
                        outputSlotIndex = sourceNode.findOutputSlot(matchingOutput.name);
                        outputSlotName = matchingOutput.name;
                    }
                }
            }

            if (inputSlotIndex === undefined && targetNode.inputs && targetNode.inputs.length > 0) {
                if (matchStrategy === "first") {
                    // Use first available (unconnected) input
                    const availableInput = targetNode.inputs.find(i => !i.link);
                    if (availableInput) {
                        inputSlotIndex = targetNode.findInputSlot(availableInput.name);
                        inputSlotName = availableInput.name;
                    } else {
                        // All connected, use first
                        inputSlotIndex = 0;
                        inputSlotName = targetNode.inputs[0].name;
                    }
                } else if (matchStrategy === "type" && outputSlotIndex !== undefined) {
                    // Match by type if we know the output type
                    const outputType = sourceNode.outputs[outputSlotIndex]?.type;
                    const matchingInput = targetNode.inputs.find(i => 
                        i.type === outputType && !i.link  // Prefer unconnected
                    );
                    if (matchingInput) {
                        inputSlotIndex = targetNode.findInputSlot(matchingInput.name);
                        inputSlotName = matchingInput.name;
                    } else {
                        // Try connected slots if no unconnected match
                        const anyMatchingInput = targetNode.inputs.find(i => i.type === outputType);
                        if (anyMatchingInput) {
                            inputSlotIndex = targetNode.findInputSlot(anyMatchingInput.name);
                            inputSlotName = anyMatchingInput.name;
                        }
                    }
                }
            }
        }

        // Check if we have both slots
        if (typeof outputSlotIndex !== "number" || typeof inputSlotIndex !== "number") {
            // Build detailed error message
            const availableOutputs = sourceNode.outputs ? 
                sourceNode.outputs.map(o => `"${o.name}" (${o.type})`).join(", ") : "none";
            const availableInputs = targetNode.inputs ?
                targetNode.inputs.map(i => `"${i.name}" (${i.type})${i.link ? ' [connected]' : ''}`).join(", ") : "none";

            const errorMsg = [
                `Could not find matching slots for connection.`,
                `Attempted: source="${sourceSlot || 'auto'}" → target="${targetSlot || 'auto'}"`,
                `Source node ${sourceNode.id} (${sourceNode.type}) outputs: ${availableOutputs}`,
                `Target node ${targetNode.id} (${targetNode.type}) inputs: ${availableInputs}`,
                ``,
                `TIP: Use get_node_slots(node_id) to discover exact slot names.`
            ].join("\n");

            throw new Error(errorMsg);
        }

        // Make the connection
        sourceNode.connect(outputSlotIndex, targetNode.id, inputSlotIndex);
        
        const connectionInfo = {
            source_node_id: sourceNode.id,
            source_slot: outputSlotName,
            source_slot_index: outputSlotIndex,
            target_node_id: targetNode.id,
            target_slot: inputSlotName,
            target_slot_index: inputSlotIndex,
            type: sourceNode.outputs[outputSlotIndex]?.type
        };
        
        console.log(
            `[FL_API] Connected: ${sourceNode.id}[${outputSlotIndex}] "${outputSlotName}" -> ` +
            `${targetNode.id}[${inputSlotIndex}] "${inputSlotName}" (${connectionInfo.type})`
        );
        
        return connectionInfo;
    } catch (error) {
        console.error("[FL_API] connect error:", error);
        throw error;
    }
}
```

---

**File**: `web/js/tool_executor.js`

**Update handler** (line ~327):

```javascript
async _handleConnectNodes(params) {
    const { 
        source_node_id, 
        source_slot, 
        target_node_id, 
        target_slot,
        auto_match,
        match_strategy
    } = params;
    
    const options = {
        auto_match: auto_match !== false,
        match_strategy: match_strategy || "type"
    };
    
    const result = this.flApi.connect(
        source_node_id,
        source_slot || null,
        target_node_id,
        target_slot || null,
        options
    );
    
    return { 
        connected: true,
        connection: result
    };
}
```

---

## Phase 3: Batch and Auto-Connect Tools

### Goal
Enable efficient multi-node connections and workflow automation.

### Success Metrics
- Agent can connect 10+ nodes in one operation
- Common workflow patterns automated
- Reduced tool call overhead

---

### 3.1: Batch Connection Tool

#### MCP Tool Definition

**File**: `backend/mcp_server.py`

```python
class ConnectionSpec(BaseModel):
    """Single connection specification."""
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title")
    source_slot: Optional[Union[str, int]] = Field(None, description="Source slot (optional)")
    target_slot: Optional[Union[str, int]] = Field(None, description="Target slot (optional)")

class ConnectNodesBatchRequest(BaseModel):
    """Request to connect multiple node pairs."""
    connections: List[ConnectionSpec] = Field(
        ..., 
        description="List of connections to make"
    )
    auto_match: bool = Field(
        True,
        description="Enable auto-matching for all connections"
    )
    stop_on_error: bool = Field(
        False,
        description="Stop on first error (false = continue and report all errors)"
    )

@mcp.tool()
async def connect_nodes_batch(request: ConnectNodesBatchRequest, ctx: Context) -> Dict[str, Any]:
    """Connect multiple node pairs in a single operation.
    
    This tool enables efficient batch connection of nodes, reducing the number
    of tool calls needed to build complex workflows.
    
    USAGE:
    connect_nodes_batch(
        connections=[
            {"source_node_id": 1, "target_node_id": 2},
            {"source_node_id": 2, "target_node_id": 3},
            {"source_node_id": 3, "target_node_id": 4}
        ]
    )
    
    RETURNS:
    {
        "total": 3,
        "successful": 2,
        "failed": 1,
        "results": [
            {"success": true, "connection": {...}},
            {"success": true, "connection": {...}},
            {"success": false, "error": "...", "attempted": {...}}
        ]
    }
    
    AGENT WORKFLOW:
    1. Plan workflow structure: A → B → C → D
    2. Call connect_nodes_batch with all connections
    3. Check results for any failures
    4. Retry failed connections with explicit slot names if needed
    """
    return await _execute_tool(ctx, "connect_nodes_batch", request.model_dump())
```

---

#### Frontend Implementation

**File**: `web/js/tool_executor.js`

**Add handler registration**:
```javascript
"connect_nodes_batch": this._handleConnectNodesBatch.bind(this),
```

**Add handler implementation**:
```javascript
async _handleConnectNodesBatch(params) {
    const { connections, auto_match, stop_on_error } = params;
    
    const results = [];
    let successful = 0;
    let failed = 0;
    
    for (const conn of connections) {
        try {
            const options = {
                auto_match: auto_match !== false,
                match_strategy: "type"
            };
            
            const result = this.flApi.connect(
                conn.source_node_id,
                conn.source_slot || null,
                conn.target_node_id,
                conn.target_slot || null,
                options
            );
            
            results.push({
                success: true,
                connection: result
            });
            successful++;
        } catch (error) {
            results.push({
                success: false,
                error: error.message,
                attempted: conn
            });
            failed++;
            
            if (stop_on_error) {
                break;
            }
        }
    }
    
    return {
        total: connections.length,
        successful,
        failed,
        results
    };
}
```

---

### 3.2: Auto-Connect Workflow Tool

#### MCP Tool Definition

**File**: `backend/mcp_server.py`

```python
class AutoConnectWorkflowRequest(BaseModel):
    """Request to auto-connect nodes in a workflow."""
    node_ids: List[Union[int, str]] = Field(
        ...,
        description="List of node IDs to connect in sequence"
    )
    strategy: Literal["sequential", "type_match", "smart"] = Field(
        "smart",
        description="Connection strategy"
    )

@mcp.tool()
async def auto_connect_workflow(request: AutoConnectWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Automatically connect nodes based on type compatibility.
    
    STRATEGIES:
    - "sequential": Connect nodes in order: A → B → C → D
    - "type_match": Connect all compatible type pairs
    - "smart": Analyze workflow and connect intelligently
    
    USAGE:
    auto_connect_workflow(
        node_ids=[1, 2, 3, 4],
        strategy="sequential"
    )
    # Connects: 1→2, 2→3, 3→4
    
    RETURNS:
    {
        "connections_made": 3,
        "connections": [
            {"source": 1, "target": 2, "type": "LATENT"},
            {"source": 2, "target": 3, "type": "IMAGE"},
            {"source": 3, "target": 4, "type": "IMAGE"}
        ],
        "failed": []
    }
    """
    return await _execute_tool(ctx, "auto_connect_workflow", request.model_dump())
```

---

## Implementation Checklist

### Phase 1: Slot Discovery & Error Improvements

**Backend** (`backend/mcp_server.py`):
- [ ] Add `GetNodeSlotsRequest` class (after line 310)
- [ ] Add `get_node_slots` tool definition (after line 520)

**Frontend** (`web/js/fl_api.js`):
- [ ] Add `getNodeSlots()` method (after line 330)
- [ ] Fix case-insensitive matching in `connect()` (replace lines 414-435)
- [ ] Improve error message in `connect()` (replace line 463)

**Frontend** (`web/js/tool_executor.js`):
- [ ] Add `get_node_slots` handler registration (line ~51)
- [ ] Add `_handleGetNodeSlots()` method (after line ~313)

**Testing**:
- [ ] Test `get_node_slots` with various node types
- [ ] Test case-insensitive slot matching ("LATENT" vs "latent")
- [ ] Verify improved error messages show available slots
- [ ] Test connection success rate improvement

---

### Phase 2: Smart Connection Enhancements

**Backend** (`backend/mcp_server.py`):
- [ ] Update `ConnectNodesRequest` with optional slots and auto-match
- [ ] Update `connect_nodes` tool docstring

**Frontend** (`web/js/fl_api.js`):
- [ ] Replace `connect()` method with enhanced version (lines 390-470)
- [ ] Add auto-matching logic (type-based, first-available)
- [ ] Return detailed connection info

**Frontend** (`web/js/tool_executor.js`):
- [ ] Update `_handleConnectNodes()` to pass options

**Testing**:
- [ ] Test auto-match with no slots provided
- [ ] Test auto-match with only source slot
- [ ] Test auto-match with only target slot
- [ ] Test different match strategies ("first", "type")
- [ ] Verify backwards compatibility

---

### Phase 3: Batch and Auto-Connect

**Backend** (`backend/mcp_server.py`):
- [ ] Add `ConnectionSpec` class
- [ ] Add `ConnectNodesBatchRequest` class
- [ ] Add `connect_nodes_batch` tool
- [ ] Add `AutoConnectWorkflowRequest` class
- [ ] Add `auto_connect_workflow` tool

**Frontend** (`web/js/tool_executor.js`):
- [ ] Add `connect_nodes_batch` handler registration
- [ ] Add `_handleConnectNodesBatch()` method
- [ ] Add `auto_connect_workflow` handler registration
- [ ] Add `_handleAutoConnectWorkflow()` method

**Frontend** (`web/js/fl_api.js`):
- [ ] Add `autoConnectWorkflow()` method (sequential strategy)
- [ ] Add type-matching algorithm

**Testing**:
- [ ] Test batch connection with 10+ nodes
- [ ] Test batch with some failures (stop_on_error=false)
- [ ] Test auto-connect sequential strategy
- [ ] Test auto-connect type_match strategy
- [ ] Verify performance improvements

---

## Success Metrics

### Before Implementation
- Connection success rate: **33%** (1/3 from logs)
- Agent requires guessing slot names
- Generic error messages
- No batch operations
- High tool call overhead

### After Phase 1
- Connection success rate: **>95%**
- Agent can discover slot names
- Detailed error messages with available slots
- Case-insensitive matching works

### After Phase 2  
- Auto-match reduces agent complexity
- Type-based matching works reliably
- Backwards compatible
- Better connection info returned

### After Phase 3
- Batch connections reduce tool calls by **5-10x**
- Common workflows can be automated
- Agent productivity significantly improved

---

## Estimated Effort

### Phase 1: 2-3 hours
- `get_node_slots` implementation: 1 hour
- Case-insensitive fix: 30 minutes
- Error message improvements: 30 minutes
- Testing: 1 hour

### Phase 2: 2-3 hours
- Enhanced `connect()` method: 1.5 hours
- Auto-matching logic: 1 hour
- Testing: 30 minutes

### Phase 3: 3-4 hours
- Batch connection tool: 1.5 hours
- Auto-connect workflow: 1.5 hours
- Testing: 1 hour

**Total: 7-10 hours**

---

## Risk Assessment

### Low Risk
- ✅ Phase 1 changes are additive (new tool + fixes)
- ✅ Case-insensitive matching is strictly better
- ✅ Error messages are non-breaking

### Medium Risk
- ⚠️ Phase 2 changes `connect()` return value (returns object instead of boolean)
  - **Mitigation**: Update handler to maintain backwards compatibility
- ⚠️ Auto-matching might connect wrong slots in edge cases
  - **Mitigation**: Make auto-match opt-in, default to type-based

### Testing Strategy
- Unit tests for each new method
- Integration tests with real ComfyUI workflows
- Agent testing with common connection scenarios
- Performance testing for batch operations

---

## Rollout Plan

### Week 1: Phase 1
1. Implement `get_node_slots` tool
2. Fix case-insensitive matching
3. Improve error messages
4. Test with real agent workflows
5. Deploy to production

### Week 2: Phase 2
1. Implement enhanced `connect()` with auto-match
2. Test backwards compatibility
3. Update agent prompts to use new features
4. Deploy to production

### Week 3: Phase 3
1. Implement batch connection tool
2. Implement auto-connect workflow
3. Performance testing
4. Deploy to production

---

## Future Enhancements (Beyond Phase 3)

### Connection Suggestions
```python
@mcp.tool()
async def suggest_connections(node_id: int) -> dict:
    """Suggest compatible connections for a node."""
```

Returns:
```json
{
    "node_id": 3,
    "suggestions": [
        {
            "target_node_id": 5,
            "source_slot": "LATENT",
            "target_slot": "samples",
            "confidence": 0.95,
            "reason": "Type match: LATENT → LATENT"
        }
    ]
}
```

### Connection Validation
```python
@mcp.tool()
async def validate_connection(source_id, source_slot, target_id, target_slot) -> dict:
    """Check if a connection is valid without making it."""
```

### Workflow Templates
```python
@mcp.tool()
async def apply_workflow_template(template: str, nodes: dict) -> dict:
    """Apply a predefined workflow template."""
```

Templates:
- "txt2img": Text-to-image workflow
- "img2img": Image-to-image workflow
- "upscale": Upscaling workflow

---

## Conclusion

This proposal provides a **comprehensive solution** to the connection tool design flaws:

1. **Phase 1** fixes immediate problems and enables slot discovery
2. **Phase 2** adds intelligent features while maintaining compatibility
3. **Phase 3** optimizes for agent efficiency with batch operations

Implementation is **low-risk**, **well-scoped**, and provides **immediate value** at each phase.

**Recommendation**: Proceed with Phase 1 implementation immediately.
