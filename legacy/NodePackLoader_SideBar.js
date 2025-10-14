import { app } from '../../scripts/app.js';

// Helper functions adapted from FL_NodePackLoader.js for sidebar context

function getNodePacksSidebar() {
    const nodeTypes = Object.keys(LiteGraph.registered_node_types);
    const nodePacks = new Map();

    nodeTypes.forEach(type => {
        const node = LiteGraph.registered_node_types[type];
        if (node && node.category) {
            const category = node.category;
            if (!nodePacks.has(category)) {
                nodePacks.set(category, []);
            }
            nodePacks.get(category).push(type);
        }
    });
    return nodePacks;
}

async function loadNodesSidebar(selectedFolder, columnCount, nodeInfoWidgets) {
    const nodePacks = getNodePacksSidebar();
    if (!selectedFolder || !nodePacks.has(selectedFolder)) {
        console.log("No folder selected or invalid folder");
        return;
    }

    const nodeTypes = nodePacks.get(selectedFolder).sort();

    if (nodeTypes.length === 0) {
        console.log(`No nodes found in category ${selectedFolder}`);
        return;
    }

    // Clear existing nodes from this category (optional, depending on desired behavior)
    // For a sidebar, we might not want to clear all nodes, but rather just add new ones.
    // For now, let's not clear to avoid unexpected behavior.

    const nodeInfo = [];
    for (const nodeType of nodeTypes) {
        const node = LiteGraph.createNode(nodeType);
        if (node) {
            app.graph.add(node);
            if (node.onNodeCreated) {
                node.onNodeCreated();
            }
            await new Promise(resolve => setTimeout(resolve, 10)); // Small delay for rendering
            nodeInfo.push({
                node: node,
                width: node.size[0],
                height: node.size[1]
            });
        }
    }

    const numColumns = Math.max(1, Math.min(columnCount || 3, nodeTypes.length));
    const nodesPerColumn = Math.ceil(nodeInfo.length / numColumns);

    const columnMetrics = Array(numColumns).fill().map(() => ({
        maxWidth: 0,
        maxHeight: 0
    }));

    nodeInfo.forEach((info, index) => {
        const columnIndex = Math.floor(index / nodesPerColumn);
        if (columnIndex < numColumns) {
            columnMetrics[columnIndex].maxWidth = Math.max(
                columnMetrics[columnIndex].maxWidth,
                info.width
            );
            columnMetrics[columnIndex].maxHeight = Math.max(
                columnMetrics[columnIndex].maxHeight,
                info.height
            );
        }
    });

    const padding = 50;
    nodeInfo.forEach((info, index) => {
        const columnIndex = Math.floor(index / nodesPerColumn);
        const positionInColumn = index % nodesPerColumn;

        let x = padding;
        for (let i = 0; i < columnIndex; i++) {
            x += columnMetrics[i].maxWidth + padding;
        }

        x += (columnMetrics[columnIndex].maxWidth - info.width) / 2;

        const y = padding + (positionInColumn * (Math.max(...columnMetrics.map(m => m.maxHeight)) + padding));

        info.node.pos = [x, y];
    });

    updateNodeInfoSidebar(selectedFolder, nodeInfoWidgets);

    const canvas = app.canvas;
    if (canvas) {
        let minX = Infinity;
        let minY = Infinity;
        let maxX = -Infinity;
        let maxY = -Infinity;

        nodeInfo.forEach(info => {
            const node = info.node;
            minX = Math.min(minX, node.pos[0]);
            minY = Math.min(minY, node.pos[1]);
            maxX = Math.max(maxX, node.pos[0] + node.size[0]);
            maxY = Math.max(maxY, node.pos[1] + node.size[1]);
        });

        const centerX = (minX + maxX) / 2;
        const centerY = (minY + maxY) / 2;
        canvas.offset = [
            canvas.width / 2 - centerX,
            canvas.height / 2 - centerY
        ];
        canvas.setZoom(0.8);
        canvas.setDirty(true, true);
    }
}

function updateNodeInfoSidebar(selectedFolder, nodeInfoWidgets) {
    const nodePacks = getNodePacksSidebar();
    const nodes = nodePacks.get(selectedFolder) || [];
    nodes.sort();

    if (nodeInfoWidgets.nodeCount) {
        nodeInfoWidgets.nodeCount.textContent = `Node Count: ${nodes.length}`;
    }
    if (nodeInfoWidgets.nodeList) {
        nodeInfoWidgets.nodeList.textContent = `Nodes:\n${nodes.join("\n")}`;
    }
}


app.extensionManager.registerSidebarTab({
  id: 'NodePackLoader_SideBar',
  icon: 'pi pi-th-large', // Changed icon to represent a grid/blocks
  title: 'NodePackLoader_SideBar', // Changed title
  tooltip: 'NodePackLoader_SideBar: Load nodes from a selected pack.', // Changed tooltip
  type: 'custom',
  render: (el) => {
    el.style.backgroundColor = '#18181b'; // Background color from user
    el.style.color = '#cccccc'; // Light grey for basic font
    el.style.height = '100%'; // Ensure container fills the sidebar height
    el.style.overflowY = 'auto'; // Enable scrolling if content overflows
    el.style.padding = '10px'; // Add some padding

    let selectedFolder = "";
    let columnCount = 3;
    let categorySearchQuery = ""; // New state for category search query

    const nodePacks = getNodePacksSidebar();
    let allCategories = Array.from(nodePacks.keys()).sort();

    // Function to update the folder select options based on search query
    const updateFolderSelectOptions = () => {
        const filteredCategories = allCategories.filter(cat =>
            cat.toLowerCase().includes(categorySearchQuery.toLowerCase())
        );
        // Clear existing options
        while (folderSelect.firstChild) {
            folderSelect.removeChild(folderSelect.firstChild);
        }
        // Add filtered options
        filteredCategories.forEach(cat => {
            const option = document.createElement('option');
            option.value = cat;
            option.textContent = cat;
            folderSelect.appendChild(option);
        });
        // Set selected folder to the first filtered category, or empty if none
        if (filteredCategories.length > 0) {
            selectedFolder = filteredCategories[0];
        } else {
            selectedFolder = "";
        }
        folderSelect.value = selectedFolder;
        updateNodeInfoSidebar(selectedFolder, { nodeCount: nodeCountDisplay, nodeList: nodeListDisplay });
    };

    // Category Search Input
    const categorySearchInput = document.createElement('input');
    categorySearchInput.type = 'text';
    categorySearchInput.placeholder = 'Search categories...';
    categorySearchInput.style.width = '100%';
    categorySearchInput.style.marginBottom = '10px';
    categorySearchInput.style.padding = '5px';
    categorySearchInput.style.backgroundColor = '#444444'; // Darker grey
    categorySearchInput.style.color = '#cccccc'; // Light grey for basic font
    categorySearchInput.style.border = '1px solid #555555'; // Border color
    categorySearchInput.addEventListener('input', (e) => {
        categorySearchQuery = e.target.value;
        updateFolderSelectOptions();
    });
    el.appendChild(categorySearchInput);

    // Node Pack Selector
    const folderSelect = document.createElement('select');
    folderSelect.style.width = '100%';
    folderSelect.style.marginBottom = '10px';
    folderSelect.style.backgroundColor = '#444444'; // Darker grey
    folderSelect.style.color = '#cccccc'; // Light grey for basic font
    folderSelect.style.border = '1px solid #555555'; // Border color
    folderSelect.addEventListener('change', (e) => {
        selectedFolder = e.target.value;
        updateNodeInfoSidebar(selectedFolder, { nodeCount: nodeCountDisplay, nodeList: nodeListDisplay });
    });
    el.appendChild(folderSelect);

    // Load Nodes Button
    const loadButton = document.createElement('button');
    loadButton.textContent = 'Load Nodes';
    loadButton.style.width = '100%';
    loadButton.style.marginBottom = '10px';
    loadButton.style.backgroundColor = '#555555'; // Button background color
    loadButton.style.color = '#ffffff'; // Button text color
    loadButton.style.border = '1px solid #666666'; // Button border color
    loadButton.onclick = () => {
        loadNodesSidebar(selectedFolder, columnCount, { nodeCount: nodeCountDisplay, nodeList: nodeListDisplay });
    };
    el.appendChild(loadButton);

    // Columns Slider
    const columnsContainer = document.createElement('div');
    columnsContainer.style.marginBottom = '10px';
    columnsContainer.style.display = 'flex';
    columnsContainer.style.alignItems = 'center';

    const columnsLabel = document.createElement('label');
    columnsLabel.textContent = 'Columns: ';
    columnsLabel.style.color = '#cccccc'; // Light grey for basic font
    columnsLabel.style.marginRight = '10px';
    columnsContainer.appendChild(columnsLabel);

    const columnsInput = document.createElement('input');
    columnsInput.type = 'range';
    columnsInput.value = columnCount;
    columnsInput.min = 1;
    columnsInput.max = 20; // Changed max to 20
    columnsInput.step = 1;
    columnsInput.style.flexGrow = '1';
    columnsInput.style.accentColor = '#555555'; // Slider thumb color
    columnsInput.addEventListener('input', (e) => {
        columnCount = Math.max(1, Math.floor(Number(e.target.value)));
        columnsValueDisplay.textContent = columnCount;
    });
    columnsContainer.appendChild(columnsInput);

    const columnsValueDisplay = document.createElement('span');
    columnsValueDisplay.textContent = columnCount;
    columnsValueDisplay.style.color = '#cccccc';
    columnsValueDisplay.style.marginLeft = '10px';
    columnsContainer.appendChild(columnsValueDisplay);

    el.appendChild(columnsContainer);

    // Node Count Display
    const nodeCountDisplay = document.createElement('div');
    nodeCountDisplay.style.color = '#cccccc'; // Light grey for basic font
    nodeCountDisplay.style.marginBottom = '5px';
    el.appendChild(nodeCountDisplay);

    // Node List Display
    const nodeListDisplay = document.createElement('textarea');
    nodeListDisplay.readOnly = true;
    nodeListDisplay.style.width = '100%';
    nodeListDisplay.style.height = '200px'; // Fixed height
    nodeListDisplay.style.resize = 'none'; // Make non-resizable
    nodeListDisplay.style.backgroundColor = '#444444'; // Darker grey
    nodeListDisplay.style.color = '#cccccc'; // Light grey for basic font
    nodeListDisplay.style.border = '1px solid #555555'; // Border color
    el.appendChild(nodeListDisplay);

    // Clear Canvas Button
    const clearCanvasButton = document.createElement('button');
    clearCanvasButton.textContent = 'Clear Canvas';
    clearCanvasButton.style.width = '100%';
    clearCanvasButton.style.marginTop = '10px'; // Add some space above the button
    clearCanvasButton.style.backgroundColor = '#555555'; // Button background color
    clearCanvasButton.style.color = '#ffffff'; // Button text color
    clearCanvasButton.style.border = '1px solid #666666'; // Button border color
    clearCanvasButton.onclick = () => {
        app.graph.clear(); // Clears all nodes from the canvas
        app.canvas.setDirty(true, true); // Redraw canvas
    };
    el.appendChild(clearCanvasButton);

    // Initial updates
    updateFolderSelectOptions(); // Populate initial categories
    updateNodeInfoSidebar(selectedFolder, { nodeCount: nodeCountDisplay, nodeList: nodeListDisplay });
  },
});