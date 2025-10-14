# Query DSL Implementation Plan

## LLM-Friendly Query Language Selection

### Analysis of Options

We evaluated three query language approaches:

1. **JSON-based DSL** ✅ SELECTED
2. SQL-like syntax
3. GraphQL-inspired

### Why JSON-based DSL?

**LLM Advantages:**
- ✅ **Native JSON generation** - LLMs are excellent at generating valid JSON
- ✅ **Structured output** - Tool schemas enforce correct structure via Pydantic
- ✅ **No syntax errors** - JSON parsing is deterministic
- ✅ **Type safety** - Pydantic validates all fields
- ✅ **Composable** - Easy to nest filters and operations
- ✅ **Clear semantics** - Each field has explicit meaning
- ✅ **Error recovery** - Invalid JSON fails fast with clear errors

**SQL-like issues:**
- ❌ LLMs can generate syntax errors (missing quotes, wrong keywords)
- ❌ Requires custom parser
- ❌ Ambiguous operator precedence
- ❌ String escaping issues

**GraphQL issues:**
- ❌ More verbose for simple queries
- ❌ Less familiar to most LLMs
- ❌ Requires GraphQL parser

## Query DSL Structure

### Core Query Object

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from enum import Enum

class FilterOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    GREATER_THAN = "gt"
    LESS_THAN = "lt"
    GREATER_EQUAL = "gte"
    LESS_EQUAL = "lte"
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"

class Filter(BaseModel):
    """Single filter condition"""
    field: str = Field(..., description="Field to filter on (e.g., 'type', 'title', 'parameters.ckpt_name')")
    operator: FilterOperator = Field(..., description="Comparison operator")
    value: Any = Field(..., description="Value to compare against")

class LogicalOperator(str, Enum):
    AND = "and"
    OR = "or"
    NOT = "not"

class FilterGroup(BaseModel):
    """Group of filters with logical operator"""
    operator: LogicalOperator = Field(default=LogicalOperator.AND)
    filters: List[Filter] = Field(default_factory=list)
    groups: List['FilterGroup'] = Field(default_factory=list, description="Nested filter groups")

class TraversalDirection(str, Enum):
    UPSTREAM = "upstream"  # Follow input connections
    DOWNSTREAM = "downstream"  # Follow output connections
    BOTH = "both"

class Traversal(BaseModel):
    """Graph traversal specification"""
    direction: TraversalDirection = Field(..., description="Direction to traverse")
    max_depth: Optional[int] = Field(None, description="Maximum traversal depth (None = unlimited)")
    node_types: Optional[List[str]] = Field(None, description="Filter traversal to specific node types")
    stop_at: Optional[FilterGroup] = Field(None, description="Stop traversal when condition met")

class AggregationType(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    LIST = "list"
    FIRST = "first"
    LAST = "last"

class Aggregation(BaseModel):
    """Aggregation operation"""
    type: AggregationType = Field(..., description="Type of aggregation")
    field: Optional[str] = Field(None, description="Field to aggregate (not needed for COUNT)")

class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"

class Sort(BaseModel):
    """Sort specification"""
    field: str = Field(..., description="Field to sort by")
    order: SortOrder = Field(default=SortOrder.ASC)

class ResultFormat(str, Enum):
    FULL = "full"  # Full node objects with all details
    SUMMARY = "summary"  # Basic info (id, type, title)
    IDS = "ids"  # Just node IDs
    SCALAR = "scalar"  # Single value (for aggregations)
    DIAGRAM = "diagram"  # Mermaid diagram

class WorkflowQuery(BaseModel):
    """Main query object"""
    filters: Optional[FilterGroup] = Field(None, description="Filter conditions")
    traversal: Optional[Traversal] = Field(None, description="Graph traversal")
    aggregation: Optional[Aggregation] = Field(None, description="Aggregation operation")
    sort: Optional[List[Sort]] = Field(None, description="Sort order")
    limit: Optional[int] = Field(None, description="Maximum number of results")
    offset: Optional[int] = Field(None, description="Number of results to skip")
    result_format: ResultFormat = Field(default=ResultFormat.FULL, description="Format of results")
    include_connections: bool = Field(default=True, description="Include connection information")
    include_position: bool = Field(default=True, description="Include position information")
```

## Query Examples

### Example 1: Find all KSampler nodes

```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {
        "field": "type",
        "operator": "equals",
        "value": "KSampler"
      }
    ]
  },
  "result_format": "full"
}
```

### Example 2: Find checkpoint loaders using SD 1.5

```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {
        "field": "type",
        "operator": "equals",
        "value": "CheckpointLoaderSimple"
      },
      {
        "field": "parameters.ckpt_name",
        "operator": "contains",
        "value": "sd15"
      }
    ]
  },
  "result_format": "summary"
}
```

### Example 3: Count all nodes of each type

```json
{
  "aggregation": {
    "type": "count"
  },
  "result_format": "scalar"
}
```

### Example 4: Get all nodes connected downstream from node 5

```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {
        "field": "id",
        "operator": "equals",
        "value": 5
      }
    ]
  },
  "traversal": {
    "direction": "downstream",
    "max_depth": null
  },
  "result_format": "full"
}
```

### Example 5: Find disconnected nodes

```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {
        "field": "connections.inputs",
        "operator": "not_exists",
        "value": null
      },
      {
        "field": "connections.outputs",
        "operator": "not_exists",
        "value": null
      }
    ]
  },
  "result_format": "summary"
}
```

### Example 6: Complex query - Find all samplers with steps > 20, sorted by position

```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {
        "field": "type",
        "operator": "in",
        "value": ["KSampler", "KSamplerAdvanced"]
      },
      {
        "field": "parameters.steps",
        "operator": "gt",
        "value": 20
      }
    ]
  },
  "sort": [
    {
      "field": "position.x",
      "order": "asc"
    },
    {
      "field": "position.y",
      "order": "asc"
    }
  ],
  "result_format": "full"
}
```

### Example 7: Get execution path from node A to node B

```json
{
  "filters": {
    "operator": "and",
    "filters": [
      {
        "field": "id",
        "operator": "equals",
        "value": 5
      }
    ]
  },
  "traversal": {
    "direction": "downstream",
    "stop_at": {
      "operator": "and",
      "filters": [
        {
          "field": "id",
          "operator": "equals",
          "value": 12
        }
      ]
    }
  },
  "result_format": "full"
}
```

## Query Execution Engine

### JavaScript Implementation

```javascript
// frontend/query_executor.js

class QueryExecutor {
    constructor(workflow) {
        this.workflow = workflow; // ComfyUI workflow object
    }
    
    execute(query) {
        // Start with all nodes
        let nodes = this.getAllNodes();
        
        // Apply filters
        if (query.filters) {
            nodes = this.applyFilters(nodes, query.filters);
        }
        
        // Apply traversal
        if (query.traversal) {
            nodes = this.applyTraversal(nodes, query.traversal);
        }
        
        // Apply sorting
        if (query.sort) {
            nodes = this.applySort(nodes, query.sort);
        }
        
        // Apply pagination
        if (query.offset || query.limit) {
            const start = query.offset || 0;
            const end = query.limit ? start + query.limit : undefined;
            nodes = nodes.slice(start, end);
        }
        
        // Apply aggregation
        if (query.aggregation) {
            return this.applyAggregation(nodes, query.aggregation);
        }
        
        // Format results
        return this.formatResults(nodes, query);
    }
    
    getAllNodes() {
        const nodes = [];
        for (const node of this.workflow.graph._nodes) {
            nodes.push(this.serializeNode(node));
        }
        return nodes;
    }
    
    applyFilters(nodes, filterGroup) {
        const { operator, filters, groups } = filterGroup;
        
        return nodes.filter(node => {
            // Evaluate individual filters
            const filterResults = filters.map(f => this.evaluateFilter(node, f));
            
            // Evaluate nested groups
            const groupResults = groups.map(g => this.applyFilters([node], g).length > 0);
            
            const allResults = [...filterResults, ...groupResults];
            
            // Apply logical operator
            if (operator === 'and') {
                return allResults.every(r => r);
            } else if (operator === 'or') {
                return allResults.some(r => r);
            } else if (operator === 'not') {
                return !allResults.every(r => r);
            }
            return false;
        });
    }
    
    evaluateFilter(node, filter) {
        const value = this.getNestedValue(node, filter.field);
        const targetValue = filter.value;
        
        switch (filter.operator) {
            case 'equals':
                return value === targetValue;
            case 'not_equals':
                return value !== targetValue;
            case 'contains':
                return String(value).includes(String(targetValue));
            case 'not_contains':
                return !String(value).includes(String(targetValue));
            case 'starts_with':
                return String(value).startsWith(String(targetValue));
            case 'ends_with':
                return String(value).endsWith(String(targetValue));
            case 'gt':
                return value > targetValue;
            case 'lt':
                return value < targetValue;
            case 'gte':
                return value >= targetValue;
            case 'lte':
                return value <= targetValue;
            case 'in':
                return Array.isArray(targetValue) && targetValue.includes(value);
            case 'not_in':
                return Array.isArray(targetValue) && !targetValue.includes(value);
            case 'exists':
                return value !== undefined && value !== null;
            case 'not_exists':
                return value === undefined || value === null;
            default:
                return false;
        }
    }
    
    getNestedValue(obj, path) {
        return path.split('.').reduce((current, key) => current?.[key], obj);
    }
    
    applyTraversal(startNodes, traversal) {
        const visited = new Set();
        const result = [];
        
        const traverse = (node, depth = 0) => {
            if (visited.has(node.id)) return;
            if (traversal.max_depth !== null && depth > traversal.max_depth) return;
            
            visited.add(node.id);
            result.push(node);
            
            // Check stop condition
            if (traversal.stop_at && this.applyFilters([node], traversal.stop_at).length > 0) {
                return;
            }
            
            // Get connected nodes
            const connections = this.getConnections(node, traversal.direction);
            
            for (const connectedId of connections) {
                const connectedNode = this.getNodeById(connectedId);
                if (!connectedNode) continue;
                
                // Filter by node type if specified
                if (traversal.node_types && !traversal.node_types.includes(connectedNode.type)) {
                    continue;
                }
                
                traverse(connectedNode, depth + 1);
            }
        };
        
        for (const node of startNodes) {
            traverse(node);
        }
        
        return result;
    }
    
    getConnections(node, direction) {
        const connections = [];
        
        if (direction === 'upstream' || direction === 'both') {
            // Get nodes connected to inputs
            for (const input of node.connections.inputs || []) {
                for (const conn of input.connected_to || []) {
                    connections.push(conn.node_id);
                }
            }
        }
        
        if (direction === 'downstream' || direction === 'both') {
            // Get nodes connected to outputs
            for (const output of node.connections.outputs || []) {
                for (const conn of output.connected_to || []) {
                    connections.push(conn.node_id);
                }
            }
        }
        
        return [...new Set(connections)];
    }
    
    applySort(nodes, sortSpecs) {
        return nodes.sort((a, b) => {
            for (const spec of sortSpecs) {
                const aVal = this.getNestedValue(a, spec.field);
                const bVal = this.getNestedValue(b, spec.field);
                
                if (aVal < bVal) return spec.order === 'asc' ? -1 : 1;
                if (aVal > bVal) return spec.order === 'asc' ? 1 : -1;
            }
            return 0;
        });
    }
    
    applyAggregation(nodes, aggregation) {
        switch (aggregation.type) {
            case 'count':
                return { count: nodes.length };
            case 'sum':
                return {
                    sum: nodes.reduce((acc, n) => 
                        acc + (this.getNestedValue(n, aggregation.field) || 0), 0
                    )
                };
            case 'avg':
                const sum = nodes.reduce((acc, n) => 
                    acc + (this.getNestedValue(n, aggregation.field) || 0), 0
                );
                return { avg: nodes.length > 0 ? sum / nodes.length : 0 };
            case 'min':
                return {
                    min: Math.min(...nodes.map(n => this.getNestedValue(n, aggregation.field)))
                };
            case 'max':
                return {
                    max: Math.max(...nodes.map(n => this.getNestedValue(n, aggregation.field)))
                };
            case 'list':
                return {
                    list: nodes.map(n => this.getNestedValue(n, aggregation.field))
                };
            case 'first':
                return nodes.length > 0 ? nodes[0] : null;
            case 'last':
                return nodes.length > 0 ? nodes[nodes.length - 1] : null;
            default:
                return { count: nodes.length };
        }
    }
    
    formatResults(nodes, query) {
        switch (query.result_format) {
            case 'ids':
                return nodes.map(n => n.id);
            
            case 'summary':
                return nodes.map(n => ({
                    id: n.id,
                    type: n.type,
                    title: n.title
                }));
            
            case 'diagram':
                return this.generateDiagram(nodes);
            
            case 'full':
            default:
                return nodes.map(n => {
                    const result = { ...n };
                    if (!query.include_connections) {
                        delete result.connections;
                    }
                    if (!query.include_position) {
                        delete result.position;
                    }
                    return result;
                });
        }
    }
    
    serializeNode(node) {
        // Convert LiteGraph node to our standard format
        return {
            id: node.id,
            type: node.type,
            title: node.title || node.type,
            position: {
                x: node.pos[0],
                y: node.pos[1]
            },
            size: {
                width: node.size[0],
                height: node.size[1]
            },
            parameters: this.extractParameters(node),
            connections: this.extractConnections(node)
        };
    }
    
    extractParameters(node) {
        const params = {};
        if (node.widgets) {
            for (const widget of node.widgets) {
                params[widget.name] = widget.value;
            }
        }
        return params;
    }
    
    extractConnections(node) {
        const inputs = [];
        const outputs = [];
        
        // Extract inputs
        if (node.inputs) {
            for (let i = 0; i < node.inputs.length; i++) {
                const input = node.inputs[i];
                const link = node.graph.links[input.link];
                inputs.push({
                    slot: input.name,
                    type: input.type,
                    connected_to: link ? [{
                        node_id: link.origin_id,
                        slot: link.origin_slot
                    }] : []
                });
            }
        }
        
        // Extract outputs
        if (node.outputs) {
            for (let i = 0; i < node.outputs.length; i++) {
                const output = node.outputs[i];
                const connectedTo = [];
                
                if (output.links) {
                    for (const linkId of output.links) {
                        const link = node.graph.links[linkId];
                        if (link) {
                            connectedTo.push({
                                node_id: link.target_id,
                                slot: link.target_slot
                            });
                        }
                    }
                }
                
                outputs.push({
                    slot: output.name,
                    type: output.type,
                    connected_to: connectedTo
                });
            }
        }
        
        return { inputs, outputs };
    }
    
    generateDiagram(nodes) {
        // Generate Mermaid diagram from nodes
        let diagram = 'graph LR\n';
        
        for (const node of nodes) {
            const nodeLabel = `N${node.id}[${node.title || node.type}]`;
            
            for (const output of node.connections.outputs || []) {
                for (const conn of output.connected_to || []) {
                    diagram += `  ${nodeLabel} -->|${output.slot}| N${conn.node_id}\n`;
                }
            }
        }
        
        return diagram;
    }
    
    getNodeById(id) {
        const node = this.workflow.graph._nodes.find(n => n.id === id);
        return node ? this.serializeNode(node) : null;
    }
}
```

## MCP Tool Definition

```python
# backend/mcp_server.py

from fastmcp import FastMCP
from pydantic import BaseModel

mcp = FastMCP("FL_JS Workflow Tools")

@mcp.tool()
async def query_workflow(query: WorkflowQuery) -> dict:
    """
    Query the workflow graph using a structured query language.
    
    Supports filtering, traversal, aggregation, and multiple result formats.
    """
    # This will be sent via WebSocket to JS client
    request_id = generate_request_id()
    
    result = await execute_tool_callback(
        request_id=request_id,
        tool_name="query_workflow",
        parameters=query.model_dump()
    )
    
    return result
```

## Agent System Prompt Examples

The agent needs examples of how to construct queries:

```
You have access to a powerful workflow query tool. Here are examples:

1. To find all nodes of a specific type:
{
  "filters": {
    "operator": "and",
    "filters": [{"field": "type", "operator": "equals", "value": "KSampler"}]
  }
}

2. To find nodes with specific parameter values:
{
  "filters": {
    "operator": "and",
    "filters": [
      {"field": "type", "operator": "equals", "value": "CheckpointLoader"},
      {"field": "parameters.ckpt_name", "operator": "contains", "value": "sd15"}
    ]
  }
}

3. To traverse connections:
{
  "filters": {
    "operator": "and",
    "filters": [{"field": "id", "operator": "equals", "value": 5}]
  },
  "traversal": {
    "direction": "downstream",
    "max_depth": null
  }
}

4. To count nodes:
{
  "aggregation": {"type": "count"},
  "result_format": "scalar"
}
```

## Summary

✅ **JSON-based DSL selected** for LLM compatibility
✅ **Comprehensive filter operators** for all query needs
✅ **Graph traversal support** for connection-based queries
✅ **Aggregation capabilities** for statistics
✅ **Multiple result formats** (full, summary, IDs, scalar, diagram)
✅ **Nested field access** via dot notation
✅ **Type-safe with Pydantic** for validation
✅ **Composable queries** with logical operators and groups
