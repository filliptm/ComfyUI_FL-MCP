/**
 * Tool Activity Visualization for FL_JS
 * Shows floating cards when tools are executing
 */

/**
 * Tool configuration registry
 * Single source of truth for tool icons, labels, and descriptions
 */
export const TOOL_CONFIG = {
    // Query & Understanding
    "workflow_overview": {
        icon: "🔍",
        label: "Overview",
        description: "Getting workflow summary and statistics"
    },
    "query_workflow": {
        icon: "🔎", 
        label: "Query",
        description: "Searching workflow nodes and connections"
    },
    "workflow_diagram": {
        icon: "📐",
        label: "Diagram",
        description: "Generating visual workflow diagram"
    },
    "frontend_list_commands": {
        icon: "⌨️",
        label: "Commands",
        description: "Reading ComfyUI frontend commands"
    },
    "frontend_execute_command": {
        icon: "▶️",
        label: "Command",
        description: "Running a ComfyUI frontend command"
    },
    "frontend_list_keybindings": {
        icon: "⌘",
        label: "Hotkeys",
        description: "Reading ComfyUI keybindings"
    },

    // Node Creation & Modification  
    "create_nodes": {
        icon: "✨",
        label: "Create",
        description: "Adding new nodes to workflow"
    },
    "remove_nodes": {
        icon: "🗑️",
        label: "Remove",
        description: "Deleting nodes from workflow"
    },
    "connect_nodes": {
        icon: "🔗",
        label: "Connect",
        description: "Linking nodes together"
    },
    
    // Selection & Focus
    "select_nodes": {
        icon: "👁️",
        label: "Select",
        description: "Selecting nodes in canvas"
    },
    "find_node": {
        icon: "🎯",
        label: "Find",
        description: "Locating specific node by criteria"
    },
    "get_current_node_selection": {
        icon: "👁️",
        label: "Get Selection",
        description: "Reading currently selected nodes"
    },
    "focus_on_nodes": {
        icon: "🔭",
        label: "Focus",
        description: "Fitting canvas view to nodes"
    },

    // Layout & Organization
    "modify_layout": {
        icon: "🏗️",
        label: "Layout",
        description: "Adjusting node positions and layout"
    },
    "get_layout": {
        icon: "📐",
        label: "Get Layout",
        description: "Reading current layout configuration"
    },

    // Workflow Execution
    "queue_workflow": {
        icon: "🚀",
        label: "Queue",
        description: "Starting workflow execution"
    },
    "cancel_workflow": {
        icon: "⏹️",
        label: "Cancel",
        description: "Stopping workflow execution"
    },
    "get_queue_status": {
        icon: "📊",
        label: "Status",
        description: "Checking execution queue status"
    },
    "enable_auto_queue": {
        icon: "🔄",
        label: "Auto Queue",
        description: "Enabling auto-queue on changes"
    },
    "disable_auto_queue": {
        icon: "⏸️",
        label: "Stop Auto",
        description: "Disabling auto-queue mode"
    },
    "set_batch_count": {
        icon: "🔢",
        label: "Batch Count",
        description: "Setting number of batch iterations"
    },
    "workflow_get_current_json": {
        icon: "🧾",
        label: "Workflow JSON",
        description: "Reading current workflow JSON"
    },
    "workflow_load_json": {
        icon: "📥",
        label: "Load Workflow",
        description: "Loading workflow JSON into the canvas"
    },
    "workflow_list_files": {
        icon: "📂",
        label: "Workflows",
        description: "Listing saved workflow files"
    },
    "workflow_save_current": {
        icon: "💾",
        label: "Save Workflow",
        description: "Saving the current workflow"
    },

    // Node Value Manipulation
    "set_node_values": {
        icon: "⚙️",
        label: "Set Values",
        description: "Updating node widget values"
    },
    "get_node_values": {
        icon: "📊",
        label: "Get Values",
        description: "Reading node widget values"
    },
    "get_node_slots": {
        icon: "🔌",
        label: "Get Slots",
        description: "Reading node input/output slots"
    },

    // Node State
    "bypass_nodes": {
        icon: "🚫",
        label: "Bypass",
        description: "Disabling nodes without removing them"
    },
    "unbypass_nodes": {
        icon: "✅",
        label: "Unbypass",
        description: "Re-enabling bypassed nodes"
    },
    "pin_nodes": {
        icon: "📌",
        label: "Pin",
        description: "Preventing nodes from moving"
    },
    "unpin_nodes": {
        icon: "📍",
        label: "Unpin",
        description: "Allowing nodes to be repositioned"
    },

    // Connection Operations
    "connect_nodes_batch": {
        icon: "🔗",
        label: "Batch Connect",
        description: "Creating multiple node connections"
    },
    "auto_connect_workflow": {
        icon: "🧩",
        label: "Auto Connect",
        description: "Automatically connecting compatible nodes"
    },

    // Utilities & Generation
    "generate_seed": {
        icon: "🌱",
        label: "Seed",
        description: "Generating random seed value"
    },
    "generate_float": {
        icon: "🎲",
        label: "Random Float",
        description: "Generating random decimal number"
    },
    "generate_int": {
        icon: "🎲",
        label: "Random Int",
        description: "Generating random integer"
    },
    "random_choice": {
        icon: "🎯",
        label: "Random Choice",
        description: "Selecting random item from list"
    },

    // System Control
    "disable_sleep": {
        icon: "☕",
        label: "Keep Awake",
        description: "Preventing system from sleeping"
    },
    "enable_sleep": {
        icon: "😴",
        label: "Allow Sleep",
        description: "Allowing system sleep again"
    },
    "disable_screensaver": {
        icon: "🖥️",
        label: "No Screensaver",
        description: "Disabling screensaver during execution"
    },
    "enable_screensaver": {
        icon: "🌙",
        label: "Screensaver",
        description: "Re-enabling screensaver"
    },
    "send_images": {
        icon: "📤",
        label: "Send Images",
        description: "Sending generated images to chat"
    },

    // Screenshot & Capture
    "take_screenshot": {
        icon: "📸",
        label: "Screenshot",
        description: "Capturing canvas as image"
    },

    // Python-only tools
    "calculate_expressions": {
        icon: "🧮",
        label: "Calculate",
        description: "Evaluating math expressions"
    },
    "wait": {
        icon: "⏳",
        label: "Wait",
        description: "Pausing execution for delay"
    },
    "get_system_info": {
        icon: "💻",
        label: "System Info",
        description: "Checking system and environment details"
    },

    // ComfyUI File Operations
    "comfy_list_folders": {
        icon: "📂",
        label: "List Folders",
        description: "Browsing ComfyUI directory structure"
    },
    "comfy_read_file": {
        icon: "📄",
        label: "Read File",
        description: "Reading ComfyUI resource files"
    },
    "comfy_search_resources": {
        icon: "🔍",
        label: "Search Resources",
        description: "Searching for models and resources"
    },

    // Node Library
    "node_library_search": {
        icon: "🔍",
        label: "Search Nodes",
        description: "Searching available node types"
    },
    "node_library_get_details": {
        icon: "📖",
        label: "Node Details",
        description: "Getting node type specifications"
    },
    "node_library_find_compatible": {
        icon: "🔗",
        label: "Find Compatible",
        description: "Finding nodes compatible with slot types"
    },

    // Manager Operations
    "manager_search_nodes": {
        icon: "🔍",
        label: "Search Manager",
        description: "Searching ComfyUI Manager database"
    },
    "manager_get_node_mappings": {
        icon: "🗺️",
        label: "Node Mappings",
        description: "Getting node-to-package mappings"
    },
    "manager_check_updates": {
        icon: "🔄",
        label: "Check Updates",
        description: "Checking for custom node updates"
    },
    "manager_queue_action": {
        icon: "🧰",
        label: "Manager Action",
        description: "Queueing a ComfyUI Manager action"
    },
    "manager_queue_status": {
        icon: "📊",
        label: "Manager Queue",
        description: "Checking Manager queue status"
    },
    "comfy_jobs_list": {
        icon: "📋",
        label: "Jobs",
        description: "Listing ComfyUI jobs"
    },
    "comfy_job_get": {
        icon: "🔍",
        label: "Job",
        description: "Reading ComfyUI job details"
    },
    "comfy_free_memory": {
        icon: "🧹",
        label: "Free Memory",
        description: "Requesting model memory cleanup"
    },
    "comfy_settings_get": {
        icon: "⚙️",
        label: "Settings",
        description: "Reading ComfyUI settings"
    },
    "comfy_settings_set": {
        icon: "⚙️",
        label: "Set Settings",
        description: "Updating ComfyUI settings"
    },

    // Error Tracking
    "get_recent_errors": {
        icon: "⚠️",
        label: "Recent Errors",
        description: "Retrieving recent execution errors"
    },
    "get_errors_for_run": {
        icon: "🔍",
        label: "Run Errors",
        description: "Getting errors for specific workflow run"
    },
    "clear_error_buffer": {
        icon: "🧹",
        label: "Clear Errors",
        description: "Clearing error tracking buffer"
    },

    // Execution Tracking
    "get_queue_status_details": {
        icon: "📊",
        label: "Queue Details",
        description: "Getting detailed queue information"
    },
    "get_execution_details": {
        icon: "🔍",
        label: "Execution Details",
        description: "Inspecting workflow execution progress"
    },

    // Generic fallback
    "*": {
        icon: "⚡",
        label: "Tool",
        description: "Executing tool operation"
    }
};

/**
 * Get tool configuration by name
 * @param {string} toolName - Name of the tool
 * @returns {object} - Icon, label, and description
 */
export function getToolConfig(toolName) {
    return TOOL_CONFIG[toolName] || TOOL_CONFIG["*"];
}
