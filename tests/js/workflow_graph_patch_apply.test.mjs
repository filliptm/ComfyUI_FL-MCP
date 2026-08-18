import assert from "node:assert/strict";
import test from "node:test";

import {
    applyWorkflowGraphPatchAtomic,
    GRAPH_PATCH_LEDGER_KEY,
    WORKFLOW_GRAPH_PATCH_PROPERTY,
    WORKFLOW_GRAPH_PATCH_SCHEMA,
} from "../../web/js/workflow_graph_patch_apply.js";
import { workflowGraphHash } from "../../web/js/graph_precondition.js";

const WORKFLOW_IDENTITY = "fl-mcp-workflow:graph-patch-test:1";
const CATALOG_HASH = "c".repeat(64);
const PATCH_HASH = "d".repeat(64);
const ATTACHMENT_SHA256 = "e".repeat(64);
const SCHEMAS = Object.freeze({
    LoadImage: "1".repeat(64),
    Nano: "2".repeat(64),
    PreviewImage: "3".repeat(64),
    WaveletColorFix: "4".repeat(64),
    SaveImage: "5".repeat(64),
    ByteDance2ReferenceNode: "6".repeat(64),
    SaveVideo: "7".repeat(64),
    GetVideoComponents: "8".repeat(64),
    VHS_VideoCombine: "9".repeat(64),
    CreateVideo: "f".repeat(64),
    Source: "a".repeat(64),
    DynamicTarget: "b".repeat(64),
    DynamicValues: "0".repeat(64),
    DynamicSlotValues: "e".repeat(64),
    AttachmentNode: "f".repeat(64),
    EmptyImage: "7".repeat(64),
});


function input(name, type, schemaIndex, socketIndex) {
    return {
        name,
        type,
        index: schemaIndex,
        schema_index: schemaIndex,
        socket_index: socketIndex,
        link: null,
    };
}


function schemaInput(name, type, index, kind = "socket") {
    return { name, type, index, kind };
}


function output(name, type, index) {
    return { name, type, index, links: [] };
}


function widget(name, type, schemaIndex, value) {
    return { name, type, schema_index: schemaIndex, value };
}


function graphNode({
    id,
    type,
    schemaHash,
    inputs = [],
    schemaInputs = null,
    outputs = [],
    widgets = [],
    values = {},
    pos = [0, 0],
    size = [240, 140],
    dynamicInputRoots = [],
}) {
    return {
        id,
        type,
        schema_hash: schemaHash,
        inputs: structuredClone(inputs),
        schema_inputs: structuredClone(schemaInputs || inputs.map(item => (
            schemaInput(item.name, item.type, item.schema_index ?? item.index, "socket")
        ))),
        outputs: structuredClone(outputs),
        widgets: structuredClone(widgets),
        values: structuredClone(values),
        widgets_values: Object.values(structuredClone(values)),
        properties: {},
        pos: structuredClone(pos),
        size: structuredClone(size),
        mode: 0,
        flags: { pinned: false },
        title: `${type} ${id}`,
        color: "#223344",
        bgcolor: "#112233",
        dynamic_input_roots: structuredClone(dynamicInputRoots),
    };
}


function link(id, source, outputIndex, outputName, target, inputIndex, inputName, type) {
    return {
        id,
        source_node_id: source,
        source_output_index: outputIndex,
        source_output: outputName,
        target_node_id: target,
        target_input_index: inputIndex,
        target_input: inputName,
        type,
    };
}


function productionWorkflow() {
    const nodes = [
        graphNode({
            id: 48,
            type: "LoadImage",
            schemaHash: SCHEMAS.LoadImage,
            outputs: [output("IMAGE", "IMAGE", 0)],
            pos: [0, 0],
        }),
        graphNode({
            id: 49,
            type: "LoadImage",
            schemaHash: SCHEMAS.LoadImage,
            outputs: [output("IMAGE", "IMAGE", 0)],
            pos: [300, 0],
        }),
        ...[50, 51, 52].map((id, offset) => graphNode({
            id,
            type: "GeminiNanoBanana2V2",
            schemaHash: SCHEMAS.Nano,
            inputs: [
                input("model.images.image_1", "IMAGE", 5, 0),
                input("model.images.image_2", "IMAGE", 6, 1),
                input("model.images.image_3", "IMAGE", 7, 2),
            ],
            schemaInputs: [
                schemaInput("prompt", "STRING", 0, "widget"),
                schemaInput("model", "COMFY_DYNAMICCOMBO_V3", 1, "widget"),
                schemaInput("model.aspect_ratio", "COMBO", 2, "widget"),
                schemaInput("model.resolution", "COMBO", 3, "widget"),
                schemaInput("model.thinking_level", "COMBO", 4, "widget"),
                schemaInput("model.images.image_1", "IMAGE", 5),
                schemaInput("model.images.image_2", "IMAGE", 6),
                schemaInput("model.images.image_3", "IMAGE", 7),
            ],
            outputs: [output("IMAGE", "IMAGE", 0)],
            pos: [600 + offset * 300, 0],
        })),
        graphNode({
            id: 53,
            type: "PreviewImage",
            schemaHash: SCHEMAS.PreviewImage,
            inputs: [input("images", "IMAGE", 0, 0)],
            pos: [900, -300],
        }),
        graphNode({
            id: 54,
            type: "PreviewImage",
            schemaHash: SCHEMAS.PreviewImage,
            inputs: [input("images", "IMAGE", 0, 0)],
            pos: [1200, -300],
        }),
        graphNode({
            id: 60,
            type: "WaveletColorFix",
            schemaHash: SCHEMAS.WaveletColorFix,
            inputs: [
                input("target_image", "IMAGE", 0, 0),
                input("source_image", "IMAGE", 1, 1),
            ],
            outputs: [output("image", "IMAGE", 0)],
            values: { align_method: "wavelet" },
            pos: [1800, 0],
        }),
        graphNode({
            id: 61,
            type: "SaveImage",
            schemaHash: SCHEMAS.SaveImage,
            inputs: [input("images", "IMAGE", 0, 0)],
            values: { filename_prefix: "ComfyUI" },
            pos: [2100, 0],
        }),
    ];
    const links = [
        link(37, 51, 0, "IMAGE", 52, 0, "model.images.image_1", "IMAGE"),
        link(38, 50, 0, "IMAGE", 51, 0, "model.images.image_1", "IMAGE"),
        link(39, 49, 0, "IMAGE", 50, 0, "model.images.image_1", "IMAGE"),
        link(40, 49, 0, "IMAGE", 52, 1, "model.images.image_2", "IMAGE"),
        link(41, 49, 0, "IMAGE", 51, 1, "model.images.image_2", "IMAGE"),
        link(42, 48, 0, "IMAGE", 51, 2, "model.images.image_3", "IMAGE"),
        link(43, 48, 0, "IMAGE", 50, 1, "model.images.image_2", "IMAGE"),
        link(44, 48, 0, "IMAGE", 52, 2, "model.images.image_3", "IMAGE"),
        link(45, 50, 0, "IMAGE", 53, 0, "images", "IMAGE"),
        link(46, 51, 0, "IMAGE", 54, 0, "images", "IMAGE"),
        link(47, 52, 0, "IMAGE", 60, 0, "target_image", "IMAGE"),
        link(48, 48, 0, "IMAGE", 60, 1, "source_image", "IMAGE"),
        link(49, 60, 0, "image", 61, 0, "images", "IMAGE"),
    ];
    return {
        id: "production-wavelet-workflow",
        version: 0.4,
        last_node_id: 61,
        last_link_id: 49,
        nodes,
        links,
        groups: [],
        config: {},
        extra: { ds: { scale: 0.65, offset: [10, 20] }, theme: "dark" },
    };
}


function createdNodeTemplate(type, id) {
    if (type === "EmptyImage") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.EmptyImage,
            schemaInputs: [
                schemaInput("width", "INT", 0, "widget"),
                schemaInput("height", "INT", 1, "widget"),
                schemaInput("batch_size", "INT", 2, "widget"),
                schemaInput("color", "INT", 3, "widget"),
            ],
            outputs: [output("IMAGE", "IMAGE", 0)],
            widgets: [
                widget("width", "INT", 0, 512),
                widget("height", "INT", 1, 512),
                widget("batch_size", "INT", 2, 1),
                widget("color", "INT", 3, 0),
            ],
            values: { width: 512, height: 512, batch_size: 1, color: 0 },
        });
    }
    if (type === "SaveImage") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.SaveImage,
            inputs: [input("images", "IMAGE", 0, 0)],
            schemaInputs: [
                schemaInput("images", "IMAGE", 0),
                schemaInput("filename_prefix", "STRING", 1, "widget"),
            ],
            widgets: [widget("filename_prefix", "STRING", 1, "ComfyUI")],
            values: { filename_prefix: "ComfyUI" },
        });
    }
    if (type === "ByteDance2ReferenceNode") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.ByteDance2ReferenceNode,
            // The live partner node materializes this AUTOGROW socket only
            // after its COMFY_DYNAMICCOMBO_V3 model branch is selected.
            inputs: [],
            schemaInputs: [
                schemaInput("model", "COMFY_DYNAMICCOMBO_V3", 0, "widget"),
                schemaInput("model.prompt", "STRING", 1, "widget"),
                schemaInput("model.resolution", "COMBO", 2, "widget"),
                schemaInput("model.ratio", "COMBO", 3, "widget"),
                schemaInput("model.duration", "INT", 4, "widget"),
                schemaInput("model.generate_audio", "BOOLEAN", 5, "widget"),
                schemaInput("model.reference_images.image_1", "IMAGE", 6),
                schemaInput("model.reference_images.image_2", "IMAGE", 7),
                schemaInput("model.auto_downscale", "BOOLEAN", 9, "widget"),
                schemaInput("model.auto_upscale", "BOOLEAN", 10, "widget"),
                schemaInput("seed", "INT", 11, "widget"),
                schemaInput("watermark", "BOOLEAN", 12, "widget"),
            ],
            outputs: [output("VIDEO", "VIDEO", 0)],
            widgets: [
                widget("model", "COMFY_DYNAMICCOMBO_V3", 0, "Seedance 2.0"),
                widget("model.prompt", "STRING", 1, ""),
                widget("model.resolution", "COMBO", 2, "1080p"),
                widget("model.ratio", "COMBO", 3, "adaptive"),
                widget("model.duration", "INT", 4, 7),
                widget("model.generate_audio", "BOOLEAN", 5, true),
                widget("model.auto_downscale", "BOOLEAN", 9, true),
                widget("model.auto_upscale", "BOOLEAN", 10, false),
                widget("seed", "INT", 11, 0),
                widget("watermark", "BOOLEAN", 12, false),
            ],
            values: {
                model: "Seedance 2.0",
                "model.prompt": "",
                "model.resolution": "1080p",
                "model.ratio": "adaptive",
                "model.duration": 7,
                "model.generate_audio": true,
                "model.auto_downscale": true,
                "model.auto_upscale": false,
                seed: 0,
                watermark: false,
            },
            dynamicInputRoots: ["model"],
        });
    }
    if (type === "SaveVideo") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.SaveVideo,
            inputs: [input("video", "VIDEO", 0, 0)],
            values: { filename_prefix: "ComfyUI" },
        });
    }
    if (type === "GetVideoComponents") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.GetVideoComponents,
            inputs: [input("video", "VIDEO", 0, 0)],
            outputs: [
                output("images", "IMAGE", 0),
                output("audio", "AUDIO", 1),
                output("fps", "FLOAT", 2),
                output("bit_depth", "INT", 3),
            ],
        });
    }
    if (type === "CreateVideo") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.CreateVideo,
            inputs: [
                input("images", "IMAGE", 0, 0),
                input("audio", "AUDIO", 2, 1),
            ],
            schemaInputs: [
                schemaInput("images", "IMAGE", 0, "socket"),
                schemaInput("fps", "FLOAT", 1, "widget"),
                schemaInput("audio", "AUDIO", 2, "socket"),
                schemaInput("bit_depth", "INT", 3, "widget"),
            ],
            outputs: [output("VIDEO", "VIDEO", 0)],
            widgets: [
                widget("fps", "FLOAT", 1, 30),
                widget("bit_depth", "INT", 3, 8),
            ],
            values: { fps: 30, bit_depth: 8 },
        });
    }
    if (type === "VHS_VideoCombine") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.VHS_VideoCombine,
            inputs: [
                input("images", "IMAGE", 0, 0),
                input("audio", "AUDIO", 7, 1),
            ],
            schemaInputs: [
                schemaInput("images", "IMAGE", 0, "socket"),
                schemaInput("frame_rate", "FLOAT", 1, "widget"),
                schemaInput("loop_count", "INT", 2, "widget"),
                schemaInput("filename_prefix", "STRING", 3, "widget"),
                schemaInput("format", "COMBO", 4, "widget"),
                schemaInput("pingpong", "BOOLEAN", 5, "widget"),
                schemaInput("save_output", "BOOLEAN", 6, "widget"),
                schemaInput("audio", "AUDIO", 7, "socket"),
            ],
            widgets: [
                widget("frame_rate", "FLOAT", 1, 24),
                widget("loop_count", "INT", 2, 0),
                widget("filename_prefix", "STRING", 3, "ComfyUI"),
                widget("format", "COMBO", 4, "video/h264-mp4"),
                widget("pingpong", "BOOLEAN", 5, false),
                widget("save_output", "BOOLEAN", 6, true),
            ],
            values: {
                frame_rate: 24,
                loop_count: 0,
                filename_prefix: "ComfyUI",
                format: "video/h264-mp4",
                pingpong: false,
                save_output: true,
            },
        });
    }
    if (type === "Source") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.Source,
            outputs: [output("IMAGE", "IMAGE", 0)],
        });
    }
    if (type === "DynamicTarget") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.DynamicTarget,
            inputs: [input("image_1", "IMAGE", 0, 0)],
            schemaInputs: [
                schemaInput("image_1", "IMAGE", 0),
                schemaInput("image_2", "IMAGE", 1),
            ],
        });
    }
    if (type === "DynamicValues") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.DynamicValues,
            schemaInputs: [
                schemaInput("selector", "COMFY_DYNAMICCOMBO_V3", 0, "widget"),
                schemaInput("selector.detail", "INT", 1, "widget"),
            ],
            widgets: [widget("selector", "COMFY_DYNAMICCOMBO_V3", 0, "basic")],
            values: { selector: "basic" },
        });
    }
    if (type === "DynamicSlotValues") {
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.DynamicSlotValues,
            inputs: [input("payload", "IMAGE", 0, 0)],
            schemaInputs: [
                {
                    ...schemaInput("payload", "COMFY_DYNAMICSLOT_V3", 0),
                    resolved_type: "IMAGE",
                    accepted_types: ["IMAGE"],
                },
                schemaInput("payload.strength", "FLOAT", 1, "widget"),
            ],
            values: {},
            dynamicInputRoots: ["payload"],
        });
    }
    if (type === "AttachmentNode") {
        const image = { filename: "old.png", subfolder: "", type: "input" };
        return graphNode({
            id,
            type,
            schemaHash: SCHEMAS.AttachmentNode,
            schemaInputs: [
                schemaInput("image", "STRING", 0, "widget"),
                schemaInput("quality", "COMBO", 1, "widget"),
            ],
            widgets: [
                widget("image", "STRING", 0, image),
                widget("quality", "COMBO", 1, "draft"),
            ],
            values: { image, quality: "draft" },
        });
    }
    throw new Error(`Unknown created node type ${type}`);
}


function sameId(left, right) {
    return String(left) === String(right);
}


class FakeGraphPatchAdapter {
    constructor(workflow = productionWorkflow()) {
        this.workflow = structuredClone(workflow);
        this.events = [];
        this.createCalls = 0;
        this.connectCalls = 0;
        this.disconnectCalls = 0;
        this.disconnectHistory = [];
        this.convertCalls = [];
        this.attachmentCalls = [];
        this.restoreCalls = 0;
        this.removeCalls = 0;
        this.failConnectAt = null;
        this.skipValue = null;
        this.sabotageUnrelatedWidgetAfterConnect = false;
        this.compactInputsOnDisconnect = false;
        this.sabotageManifestOnSet = null;
        this.sabotageConnectionTypeAt = null;
        this.resizeOnSetNodeId = null;
        this.resizeOnConnectTargetId = null;
        this.damageUnrelatedRectOnConnectNodeId = null;
        this.seedanceReferenceMaterializations = 0;
        this.seedanceReferencePrefixInputs = [];
        this.seedanceReferenceDuplicateCount = 1;
        this.seedanceReferenceType = "IMAGE";
        this.seedanceReferenceSchemaType = "IMAGE";
        this.seedanceMaterializeSecondOnFirst = false;
        this.sourceOutputType = "IMAGE";
    }

    async withReadGuard(operation) {
        return await operation();
    }

    captureWorkflow() {
        return structuredClone(this.workflow);
    }

    restoreWorkflow(snapshot) {
        this.restoreCalls += 1;
        this.workflow = structuredClone(snapshot);
    }

    getNode(id) {
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        if (!node) return null;
        return {
            ...structuredClone(node),
            node_id: node.id,
            node_type: node.type,
            position: { x: node.pos[0], y: node.pos[1] },
            size: { width: node.size[0], height: node.size[1] },
            live_inputs: structuredClone(node.inputs),
            serialized_node: structuredClone(node),
        };
    }

    listConnections() {
        return structuredClone(this.workflow.links);
    }

    createNode(planned) {
        this.createCalls += 1;
        const id = this.workflow.last_node_id + 1;
        this.workflow.last_node_id = id;
        const created = createdNodeTemplate(planned.node_type, id);
        if (created.type === "Source") {
            created.outputs[0].type = this.sourceOutputType;
        }
        if (created.type === "ByteDance2ReferenceNode") {
            for (const schema of created.schema_inputs) {
                if (schema.name.startsWith("model.reference_images.image_")) {
                    schema.type = this.seedanceReferenceSchemaType;
                }
            }
        }
        this.workflow.nodes.push(created);
        return { id, node_id: id };
    }

    setNodeValuesExact(id, values) {
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        if (!node) throw new Error("value node missing");
        const applied = [];
        for (const [name, value] of Object.entries(values)) {
            if (name === this.skipValue) continue;
            if (
                ["DynamicValues", "DynamicSlotValues"].includes(node.type)
                && !node.widgets.some(item => item.name === name)
            ) continue;
            node.values[name] = structuredClone(value);
            const targetWidget = node.widgets.find(item => item.name === name);
            if (targetWidget) targetWidget.value = structuredClone(value);
            applied.push(name);
            if (
                node.type === "ByteDance2ReferenceNode"
                && name === "model"
                && value === "Seedance 2.0"
                && !node.inputs.some(item => item.name === "model.reference_images.image_1")
            ) {
                for (const prefixed of this.seedanceReferencePrefixInputs) {
                    node.inputs.push({
                        ...structuredClone(prefixed),
                        socket_index: node.inputs.length,
                    });
                }
                for (let index = 0; index < this.seedanceReferenceDuplicateCount; index += 1) {
                    node.inputs.push(input(
                        "model.reference_images.image_1",
                        this.seedanceReferenceType,
                        6,
                        node.inputs.length,
                    ));
                }
                this.seedanceReferenceMaterializations += 1;
            }
            if (
                node.type === "DynamicValues"
                && name === "selector"
                && value === "advanced"
                && !node.widgets.some(item => item.name === "selector.detail")
            ) {
                node.widgets.push(widget("selector.detail", "INT", 1, 0));
                node.values["selector.detail"] = 0;
                node.inputs.push(input("selector.payload", "IMAGE", 2, node.inputs.length));
            }
        }
        if (sameId(id, this.resizeOnSetNodeId)) {
            node.size = [node.size[0] + 120, node.size[1] + 80];
        }
        if (sameId(id, this.sabotageManifestOnSet?.node_id)) {
            if (this.sabotageManifestOnSet.manifest === "output") node.outputs.pop();
            if (this.sabotageManifestOnSet.manifest === "input") node.inputs.pop();
        }
        node.widgets_values = Object.values(structuredClone(node.values));
        return { applied };
    }

    setNodeMetadata(id, metadata) {
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        if (!node) throw new Error("metadata node missing");
        node.properties[WORKFLOW_GRAPH_PATCH_PROPERTY] = structuredClone(metadata);
    }

    setNodeLayoutExact(id, layout) {
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        node.pos = [layout.x, layout.y];
        if (layout.width !== undefined) node.size[0] = layout.width;
        if (layout.height !== undefined) node.size[1] = layout.height;
    }

    assignAttachmentExact(id, attachment) {
        this.attachmentCalls.push(structuredClone(attachment));
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        node.values[attachment.input] = {
            filename: attachment.filename,
            subfolder: attachment.subfolder,
            type: attachment.file_type,
        };
        return { assigned: true };
    }

    verifyAttachmentExact(id, attachment) {
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        return JSON.stringify(node?.values?.[attachment.input]) === JSON.stringify({
            filename: attachment.filename,
            subfolder: attachment.subfolder,
            type: attachment.file_type,
        });
    }

    disconnectConnection(expected) {
        this.disconnectCalls += 1;
        this.disconnectHistory.push(structuredClone(expected));
        const matches = this.workflow.links
            .map((edge, index) => ({ edge, index }))
            .filter(({ edge }) => (
                sameId(edge.source_node_id, expected.source_node_id)
                && edge.source_output_index === expected.source_output_index
                && sameId(edge.target_node_id, expected.target_node_id)
                && edge.target_input_index === expected.target_input_index
            ));
        if (matches.length !== 1) throw new Error(`disconnect found ${matches.length}`);
        this.workflow.links.splice(matches[0].index, 1);
        if (this.compactInputsOnDisconnect) {
            const target = this.workflow.nodes.find(item => sameId(item.id, expected.target_node_id));
            target?.inputs.splice(expected.target_input_index, 1);
            target?.inputs.forEach((item, index) => {
                item.socket_index = index;
            });
            for (const remaining of this.workflow.links) {
                if (
                    sameId(remaining.target_node_id, expected.target_node_id)
                    && remaining.target_input_index > expected.target_input_index
                ) remaining.target_input_index -= 1;
            }
        }
    }

    convertWidgetToInput(id, specification) {
        const node = this.workflow.nodes.find(item => sameId(item.id, id));
        const widgetIndex = node.widgets.findIndex(item => (
            item.name === specification.input
            && item.type === specification.type
            && item.schema_index === specification.input_index
        ));
        if (widgetIndex < 0) throw new Error("convertible widget missing");
        const socketIndex = node.inputs.length;
        node.widgets.splice(widgetIndex, 1);
        node.inputs.push(input(
            specification.input,
            specification.type,
            specification.input_index,
            socketIndex,
        ));
        this.convertCalls.push({ id, specification: structuredClone(specification), socket_index: socketIndex });
        return { socket_index: socketIndex };
    }

    connectNodes(sourceId, targetId, connection) {
        this.connectCalls += 1;
        if (this.failConnectAt === this.connectCalls) throw new Error("injected connection failure");
        const target = this.workflow.nodes.find(item => sameId(item.id, targetId));
        const targetInput = target?.inputs.find(item => item.socket_index === connection.target_input_index);
        if (!targetInput || targetInput.name !== connection.target_input) {
            throw new Error("exact target socket missing");
        }
        if (this.workflow.links.some(edge => (
            sameId(edge.target_node_id, targetId)
            && edge.target_input_index === connection.target_input_index
        ))) throw new Error("target occupied");
        const id = this.workflow.last_link_id + 1;
        this.workflow.last_link_id = id;
        this.workflow.links.push({ id, ...structuredClone(connection) });
        if (this.sabotageConnectionTypeAt === this.connectCalls) {
            this.workflow.links.at(-1).type = "MISMATCH";
        }
        if (this.sabotageUnrelatedWidgetAfterConnect) {
            this.workflow.nodes[0].widgets_values = ["tampered"];
        }
        if (
            target.type === "DynamicTarget"
            && connection.target_input === "image_1"
            && target.schema_inputs.some(item => item.name === "image_2")
        ) {
            const hasSecond = target.inputs.some(item => item.name === "image_2");
            if (!hasSecond) target.inputs.push(input("image_2", "IMAGE", 1, 1));
        }
        if (
            target.type === "ByteDance2ReferenceNode"
            && connection.target_input === "model.reference_images.image_1"
            && this.seedanceMaterializeSecondOnFirst
            && !target.inputs.some(item => item.name === "model.reference_images.image_2")
        ) {
            target.inputs.push(input(
                "model.reference_images.image_2",
                "IMAGE",
                7,
                target.inputs.length,
            ));
        }
        if (
            target.type === "DynamicSlotValues"
            && connection.target_input === "payload"
            && !target.widgets.some(item => item.name === "payload.strength")
        ) {
            target.widgets.push(widget("payload.strength", "FLOAT", 1, 0.5));
            target.values["payload.strength"] = 0.5;
            target.widgets_values = Object.values(structuredClone(target.values));
        }
        if (sameId(targetId, this.resizeOnConnectTargetId)) {
            target.size = [target.size[0] + 90, target.size[1] + 45];
        }
        if (this.damageUnrelatedRectOnConnectNodeId !== null) {
            const unrelated = this.workflow.nodes.find(item => (
                sameId(item.id, this.damageUnrelatedRectOnConnectNodeId)
            ));
            if (unrelated) {
                unrelated.pos = [unrelated.pos[0] + 17, unrelated.pos[1] - 11];
                unrelated.size = [unrelated.size[0] + 33, unrelated.size[1] + 21];
            }
        }
        return { id };
    }

    removeNodes(ids) {
        this.removeCalls += 1;
        const keys = new Set(ids.map(String));
        this.workflow.nodes = this.workflow.nodes.filter(node => !keys.has(String(node.id)));
        this.workflow.links = this.workflow.links.filter(edge => (
            !keys.has(String(edge.source_node_id))
            && !keys.has(String(edge.target_node_id))
        ));
    }

    setWorkflowExtra(key, value) {
        this.workflow.extra ||= {};
        this.workflow.extra[key] = structuredClone(value);
    }

    afterMutationStep(step) {
        this.events.push(structuredClone(step));
    }
}


function existingRef(nodeId) {
    return { node_id: nodeId };
}


function newRef(alias) {
    return { alias };
}


function source(ref, outputIndex, name, type) {
    return { ref, output_index: outputIndex, output: name, type };
}


function target(ref, inputIndex, socketIndex, name, type, mode = "slot", occurrence = 0) {
    return {
        ref,
        input_index: inputIndex,
        occurrence_index: occurrence,
        socket_index: socketIndex,
        input: name,
        type,
        mode,
    };
}


function edge(sourceEndpoint, targetEndpoint) {
    return { source: sourceEndpoint, target: targetEndpoint };
}


function assertion(nodeId, nodeTypeValue, schemaHash) {
    return { ref: existingRef(nodeId), node_type: nodeTypeValue, schema_hash: schemaHash };
}


async function productionPatch(adapter, overrides = {}) {
    const addEdges = [
        edge(
            source(newRef("seedance_reference"), 0, "VIDEO", "VIDEO"),
            target(newRef("save_video"), 0, 0, "video", "VIDEO"),
        ),
        edge(
            source(newRef("seedance_reference"), 0, "VIDEO", "VIDEO"),
            target(newRef("video_components"), 0, 0, "video", "VIDEO"),
        ),
        edge(
            source(newRef("video_components"), 1, "audio", "AUDIO"),
            target(newRef("video_combine"), 7, 1, "audio", "AUDIO"),
        ),
        edge(
            source(newRef("video_components"), 2, "fps", "FLOAT"),
            target(newRef("video_combine"), 1, null, "frame_rate", "FLOAT", "convert_widget"),
        ),
        edge(
            source(existingRef(60), 0, "image", "IMAGE"),
            target(
                newRef("seedance_reference"),
                6,
                0,
                "model.reference_images.image_1",
                "IMAGE",
            ),
        ),
        edge(
            source(newRef("video_components"), 0, "images", "IMAGE"),
            target(newRef("video_combine"), 0, 0, "images", "IMAGE"),
        ),
    ];
    return {
        application_id: "ren-graph-patch-seedance-vhs-20260808-v1",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: PATCH_HASH,
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            assertions: {
                nodes: [assertion(60, "WaveletColorFix", SCHEMAS.WaveletColorFix)],
                edges: [],
            },
            create_nodes: [
                {
                    alias: "save_video",
                    node_type: "SaveVideo",
                    schema_hash: SCHEMAS.SaveVideo,
                    values: {
                        codec: "auto",
                        filename_prefix: "video/ComfyUI",
                        format: "auto",
                    },
                },
                {
                    alias: "seedance_reference",
                    node_type: "ByteDance2ReferenceNode",
                    schema_hash: SCHEMAS.ByteDance2ReferenceNode,
                    values: {
                        model: "Seedance 2.0",
                        "model.auto_downscale": true,
                        "model.auto_upscale": false,
                        "model.duration": 7,
                        "model.generate_audio": true,
                        "model.prompt": "Preserve the subject and animate the approved image.",
                        "model.ratio": "adaptive",
                        "model.resolution": "1080p",
                        seed: 0,
                        watermark: false,
                    },
                    layout_hint: { x: 3000, y: -400, width: 420, height: 420 },
                },
                {
                    alias: "video_combine",
                    node_type: "VHS_VideoCombine",
                    schema_hash: SCHEMAS.VHS_VideoCombine,
                    values: {
                        filename_prefix: "AnimateDiff",
                        format: "video/h264-mp4",
                        loop_count: 0,
                        pingpong: false,
                        save_output: true,
                    },
                },
                {
                    alias: "video_components",
                    node_type: "GetVideoComponents",
                    schema_hash: SCHEMAS.GetVideoComponents,
                    values: {},
                },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: addEdges,
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 4,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 6,
                removed_edge_count: 0,
                final_node_count: 13,
                final_edge_count: 19,
            },
            ...structuredClone(overrides),
        },
    };
}


async function nativeVideoConverterBundlePatch(adapter, overrides = {}) {
    const addEdges = [
        // Deliberately reverse semantic order. GraphPatch must derive the DAG
        // order rather than trusting the request array.
        edge(
            source(newRef("create_video"), 0, "VIDEO", "VIDEO"),
            target(newRef("save_video"), 0, 0, "video", "VIDEO"),
        ),
        edge(
            source(newRef("video_components"), 3, "bit_depth", "INT"),
            target(newRef("create_video"), 3, null, "bit_depth", "INT", "convert_widget"),
        ),
        edge(
            source(newRef("video_components"), 1, "audio", "AUDIO"),
            target(newRef("vhs_bundle"), 7, 1, "audio", "AUDIO"),
        ),
        edge(
            source(existingRef(60), 0, "image", "IMAGE"),
            target(
                newRef("seedance_reference"),
                6,
                0,
                "model.reference_images.image_1",
                "IMAGE",
            ),
        ),
        edge(
            source(newRef("video_components"), 2, "fps", "FLOAT"),
            target(newRef("vhs_bundle"), 1, null, "frame_rate", "FLOAT", "convert_widget"),
        ),
        edge(
            source(newRef("seedance_reference"), 0, "VIDEO", "VIDEO"),
            target(newRef("video_components"), 0, 0, "video", "VIDEO"),
        ),
        edge(
            source(newRef("video_components"), 0, "images", "IMAGE"),
            target(newRef("create_video"), 0, 0, "images", "IMAGE"),
        ),
        edge(
            source(newRef("video_components"), 1, "audio", "AUDIO"),
            target(newRef("create_video"), 2, 1, "audio", "AUDIO"),
        ),
        edge(
            source(newRef("video_components"), 2, "fps", "FLOAT"),
            target(newRef("create_video"), 1, null, "fps", "FLOAT", "convert_widget"),
        ),
        edge(
            source(newRef("video_components"), 0, "images", "IMAGE"),
            target(newRef("vhs_bundle"), 0, 0, "images", "IMAGE"),
        ),
    ];
    return {
        application_id: "ren-graph-patch-native-video-converter-v1",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "8".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            assertions: {
                nodes: [assertion(60, "WaveletColorFix", SCHEMAS.WaveletColorFix)],
                edges: [],
            },
            // Deliberately declare sinks and downstream converters first.
            create_nodes: [
                {
                    alias: "save_video",
                    node_type: "SaveVideo",
                    schema_hash: SCHEMAS.SaveVideo,
                    values: {
                        codec: "auto",
                        filename_prefix: "video/ren-native-converter",
                        format: "auto",
                    },
                },
                {
                    alias: "create_video",
                    node_type: "CreateVideo",
                    schema_hash: SCHEMAS.CreateVideo,
                    values: {},
                },
                {
                    alias: "vhs_bundle",
                    node_type: "VHS_VideoCombine",
                    schema_hash: SCHEMAS.VHS_VideoCombine,
                    values: {
                        filename_prefix: "ren-vhs-bundle",
                        format: "video/h264-mp4",
                        loop_count: 0,
                        pingpong: false,
                        save_output: true,
                    },
                },
                {
                    alias: "video_components",
                    node_type: "GetVideoComponents",
                    schema_hash: SCHEMAS.GetVideoComponents,
                    values: {},
                },
                {
                    alias: "seedance_reference",
                    node_type: "ByteDance2ReferenceNode",
                    schema_hash: SCHEMAS.ByteDance2ReferenceNode,
                    // Put branch-dependent values before their selector so the
                    // executor must settle the dynamic branch deterministically.
                    values: {
                        "model.prompt": "Animate the approved Wavelet frame.",
                        "model.resolution": "1080p",
                        "model.ratio": "adaptive",
                        "model.duration": 7,
                        "model.generate_audio": true,
                        "model.auto_downscale": true,
                        "model.auto_upscale": false,
                        seed: 0,
                        watermark: false,
                        model: "Seedance 2.0",
                    },
                },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: addEdges,
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 5,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 10,
                removed_edge_count: 0,
                final_node_count: 14,
                final_edge_count: 23,
            },
            ...structuredClone(overrides),
        },
    };
}


function emptyWorkflow() {
    return {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
}


async function seedanceAutogrowPatch(adapter, {
    includeSecondImage = false,
    endpointType = "IMAGE",
} = {}) {
    const createNodes = [
        { alias: "source_one", node_type: "Source", schema_hash: SCHEMAS.Source, values: {} },
        ...(includeSecondImage ? [
            { alias: "source_two", node_type: "Source", schema_hash: SCHEMAS.Source, values: {} },
        ] : []),
        {
            alias: "seedance",
            node_type: "ByteDance2ReferenceNode",
            schema_hash: SCHEMAS.ByteDance2ReferenceNode,
            values: { model: "Seedance 2.0" },
        },
    ];
    const addEdges = [
        ...(includeSecondImage ? [edge(
            source(newRef("source_two"), 0, "IMAGE", endpointType),
            target(
                newRef("seedance"),
                7,
                1,
                "model.reference_images.image_2",
                endpointType,
            ),
        )] : []),
        edge(
            source(newRef("source_one"), 0, "IMAGE", endpointType),
            target(
                newRef("seedance"),
                6,
                0,
                "model.reference_images.image_1",
                endpointType,
            ),
        ),
    ];
    return {
        application_id: includeSecondImage
            ? "seedance-autogrow-second-slot"
            : "seedance-autogrow-semantic-rebind",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: (includeSecondImage ? "4" : "3").repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            assertions: { nodes: [], edges: [] },
            create_nodes: createNodes,
            update_nodes: [],
            remove_edges: [],
            add_edges: addEdges,
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: createNodes.length,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: addEdges.length,
                removed_edge_count: 0,
                final_node_count: createNodes.length,
                final_edge_count: addEdges.length,
            },
        },
    };
}


function existingEdgeWorkflow({ withUnrelated = false } = {}) {
    const nodes = [
        graphNode({
            id: 1,
            type: "Source",
            schemaHash: SCHEMAS.Source,
            outputs: [output("IMAGE", "IMAGE", 0)],
            pos: [40, 60],
            size: [210, 120],
        }),
        graphNode({
            id: 2,
            type: "DynamicTarget",
            schemaHash: SCHEMAS.DynamicTarget,
            inputs: [input("image_1", "IMAGE", 0, 0)],
            schemaInputs: [
                schemaInput("image_1", "IMAGE", 0),
                schemaInput("image_2", "IMAGE", 1),
            ],
            pos: [420, 80],
            size: [260, 160],
        }),
    ];
    if (withUnrelated) {
        nodes.push(graphNode({
            id: 3,
            type: "Source",
            schemaHash: SCHEMAS.Source,
            outputs: [output("IMAGE", "IMAGE", 0)],
            pos: [900, 300],
            size: [190, 110],
        }));
    }
    return {
        version: 0.4,
        last_node_id: withUnrelated ? 3 : 2,
        last_link_id: 0,
        nodes,
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
}


async function existingEdgePatch(adapter, {
    applicationId,
    updateNodes = [],
    finalNodeCount = 2,
} = {}) {
    return {
        application_id: applicationId,
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "6".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "DynamicTarget", SCHEMAS.DynamicTarget),
                ],
                edges: [],
            },
            create_nodes: [],
            update_nodes: updateNodes,
            remove_edges: [],
            add_edges: [edge(
                source(existingRef(1), 0, "IMAGE", "IMAGE"),
                target(existingRef(2), 0, 0, "image_1", "IMAGE"),
            )],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: updateNodes.length,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 0,
                final_node_count: finalNodeCount,
                final_edge_count: 1,
            },
        },
    };
}


test("GraphPatch v2 builds the production Seedance/VHS DAG and preserves Wavelet SaveImage", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const before = adapter.captureWorkflow();
    const request = await productionPatch(adapter);

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(result.applied, true);
    assert.equal(result.queued, false);
    assert.deepEqual(result.aliases, {
        seedance_reference: 62,
        save_video: 63,
        video_components: 64,
        video_combine: 65,
    });
    assert.equal(before.nodes.length, 9);
    assert.equal(before.links.length, 13);
    assert.equal(adapter.workflow.nodes.length, 13);
    assert.equal(adapter.workflow.links.length, 19);
    assert.deepEqual(adapter.workflow.nodes.slice(0, 9), before.nodes);
    assert.deepEqual(adapter.workflow.links.slice(0, 13), before.links);
    assert.deepEqual(adapter.workflow.links[12], link(49, 60, 0, "image", 61, 0, "images", "IMAGE"));

    const added = adapter.workflow.links.slice(13);
    assert.equal(added.filter(item => item.source_node_id === 62).length, 2);
    assert.ok(added.some(item => (
        item.source_node_id === 64
        && item.source_output === "audio"
        && item.target_node_id === 65
        && item.target_input === "audio"
    )));
    assert.ok(added.some(item => (
        item.source_node_id === 64
        && item.source_output === "fps"
        && item.target_node_id === 65
        && item.target_input === "frame_rate"
        && item.target_input_index === 2
    )));
    assert.equal(adapter.convertCalls.length, 1);
    assert.deepEqual(adapter.convertCalls[0], {
        id: 65,
        specification: {
            input_index: 1,
            occurrence_index: 0,
            input: "frame_rate",
            type: "FLOAT",
        },
        socket_index: 2,
    });

    const outgoing = new Set(added.map(item => item.source_node_id));
    assert.equal(outgoing.has(63), false, "SaveVideo remains a sink");
    assert.equal(outgoing.has(65), false, "VHS remains a second sink");
    assert.equal(result.verification.valid, true);
    assert.equal(result.verification.preserved_edge_count, 13);
    assert.equal(result.reveal_delay_ms, 100);
    assert.deepEqual(
        adapter.events.map(item => item.phase),
        [
            "node", "node", "node", "node",
            "connection", "connection", "connection", "connection",
            "convert_widget", "connection", "connection",
        ],
    );
    assert.deepEqual(
        adapter.events.filter(item => item.phase === "node").map(item => item.alias),
        ["seedance_reference", "save_video", "video_components", "video_combine"],
    );
    assert.ok(
        adapter.events.filter(item => item.phase === "node").every(item => item.delay_ms === 0),
        "node creation already paced itself via the normalization guard wait",
    );
    assert.ok(
        adapter.events.filter(item => item.phase !== "node").every(item => item.delay_ms === 100),
    );
    assert.equal(adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY].schema, WORKFLOW_GRAPH_PATCH_SCHEMA);
});


test("GraphPatch applies the live-shaped CreateVideo converter and direct Seedance/VHS bundle", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const before = adapter.captureWorkflow();
    const request = await nativeVideoConverterBundlePatch(adapter);

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true, JSON.stringify(result));
    assert.equal(result.applied, true);
    assert.equal(result.queued, false);
    assert.equal(result.rollback.attempted, false);
    assert.equal(result.verification.valid, true);
    assert.deepEqual(result.aliases, {
        seedance_reference: 62,
        video_components: 63,
        create_video: 64,
        save_video: 65,
        vhs_bundle: 66,
    });
    assert.equal(adapter.seedanceReferenceMaterializations, 1);
    assert.equal(adapter.workflow.nodes.length, 14);
    assert.equal(adapter.workflow.links.length, 23);
    assert.deepEqual(adapter.workflow.nodes.slice(0, before.nodes.length), before.nodes);
    assert.deepEqual(adapter.workflow.links.slice(0, before.links.length), before.links);
    assert.equal(adapter.workflow.extra.theme, "dark");

    const added = adapter.workflow.links.slice(before.links.length);
    const topology = added.map(item => [
        item.source_node_id,
        item.source_output_index,
        item.source_output,
        item.target_node_id,
        item.target_input_index,
        item.target_input,
        item.type,
    ].join("|"));
    assert.deepEqual(new Set(topology), new Set([
        "60|0|image|62|0|model.reference_images.image_1|IMAGE",
        "62|0|VIDEO|63|0|video|VIDEO",
        "63|0|images|64|0|images|IMAGE",
        "63|2|fps|64|2|fps|FLOAT",
        "63|1|audio|64|1|audio|AUDIO",
        "63|3|bit_depth|64|3|bit_depth|INT",
        "64|0|VIDEO|65|0|video|VIDEO",
        "63|0|images|66|0|images|IMAGE",
        "63|2|fps|66|2|frame_rate|FLOAT",
        "63|1|audio|66|1|audio|AUDIO",
    ]));
    assert.deepEqual(adapter.convertCalls, [
        {
            id: 64,
            specification: {
                input_index: 1,
                occurrence_index: 0,
                input: "fps",
                type: "FLOAT",
            },
            socket_index: 2,
        },
        {
            id: 64,
            specification: {
                input_index: 3,
                occurrence_index: 0,
                input: "bit_depth",
                type: "INT",
            },
            socket_index: 3,
        },
        {
            id: 66,
            specification: {
                input_index: 1,
                occurrence_index: 0,
                input: "frame_rate",
                type: "FLOAT",
            },
            socket_index: 2,
        },
    ]);
    assert.deepEqual(
        adapter.events.filter(item => item.phase === "node").map(item => item.alias),
        ["seedance_reference", "video_components", "create_video", "save_video", "vhs_bundle"],
    );
    const firstConnection = adapter.events.find(item => item.phase === "connection");
    assert.equal(firstConnection.edge.target.input, "model.reference_images.image_1");
    assert.equal(adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY].schema, WORKFLOW_GRAPH_PATCH_SCHEMA);
});


test("Seedance AUTOGROW binds the exact dynamic path when its live socket index differs from the projection", async () => {
    const adapter = new FakeGraphPatchAdapter(emptyWorkflow());
    adapter.seedanceReferencePrefixInputs = [
        input("model.reference_videos.video_1", "VIDEO", 8, 0),
    ];
    const request = await seedanceAutogrowPatch(adapter);

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true, JSON.stringify(result));
    assert.equal(result.queued, false);
    assert.equal(result.verification.valid, true);
    assert.equal(adapter.workflow.links.length, 1);
    assert.equal(adapter.workflow.links[0].target_input, "model.reference_images.image_1");
    assert.equal(adapter.workflow.links[0].target_input_index, 1);
    assert.equal(
        adapter.workflow.nodes.find(node => node.type === "ByteDance2ReferenceNode")
            .inputs[1].name,
        "model.reference_images.image_1",
    );
});


test("dynamic semantic binding accepts only trusted polymorphic and canonical union markers", async () => {
    const cases = [
        { label: "wildcard", liveType: "*", endpointType: "IMAGE" },
        { label: "matchtype", liveType: "COMFY_MATCHTYPE_V3", endpointType: "IMAGE" },
        { label: "union", liveType: "FLOAT,INT,BOOLEAN", endpointType: "FLOAT" },
    ];
    for (const scenario of cases) {
        const adapter = new FakeGraphPatchAdapter(emptyWorkflow());
        adapter.seedanceReferencePrefixInputs = [
            input("model.reference_videos.video_1", "VIDEO", 8, 0),
        ];
        adapter.seedanceReferenceType = scenario.liveType;
        adapter.seedanceReferenceSchemaType = scenario.endpointType;
        adapter.sourceOutputType = scenario.endpointType;
        const request = await seedanceAutogrowPatch(adapter, {
            endpointType: scenario.endpointType,
        });

        const result = await applyWorkflowGraphPatchAtomic(request, adapter);

        assert.equal(result.success, true, `${scenario.label}: ${JSON.stringify(result)}`);
        assert.equal(result.verification.valid, true, scenario.label);
        assert.equal(adapter.workflow.links[0].target_input_index, 1, scenario.label);
        assert.equal(adapter.workflow.links[0].type, scenario.endpointType, scenario.label);
    }
});


test("Seedance AUTOGROW keeps a missing second image pending until image_1 materializes it", async () => {
    const adapter = new FakeGraphPatchAdapter(emptyWorkflow());
    adapter.seedanceReferencePrefixInputs = [
        input("model.reference_videos.video_1", "VIDEO", 8, 0),
    ];
    adapter.seedanceMaterializeSecondOnFirst = true;
    const request = await seedanceAutogrowPatch(adapter, { includeSecondImage: true });

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true, JSON.stringify(result));
    assert.equal(result.verification.valid, true);
    assert.deepEqual(
        adapter.workflow.links.map(item => [item.target_input, item.target_input_index]),
        [
            ["model.reference_images.image_1", 1],
            ["model.reference_images.image_2", 2],
        ],
    );
});


test("Seedance AUTOGROW fails closed for ambiguous or wrong-typed semantic sockets", async () => {
    for (const scenario of [
        "ambiguous",
        "wrong_type",
        "malformed_union",
        "foreign_union",
    ]) {
        const adapter = new FakeGraphPatchAdapter(emptyWorkflow());
        const before = adapter.captureWorkflow();
        if (scenario === "ambiguous") adapter.seedanceReferenceDuplicateCount = 2;
        if (scenario === "wrong_type") adapter.seedanceReferenceType = "VIDEO";
        if (scenario === "malformed_union") adapter.seedanceReferenceType = "IMAGE, VIDEO";
        if (scenario === "foreign_union") adapter.seedanceReferenceType = "VIDEO,AUDIO";
        const request = await seedanceAutogrowPatch(adapter);

        const result = await applyWorkflowGraphPatchAtomic(request, adapter);

        assert.equal(result.success, false, scenario);
        assert.equal(result.error.code, "graph_patch_slot_mismatch", scenario);
        assert.equal(result.rollback.complete, true, scenario);
        assert.deepEqual(adapter.workflow, before, scenario);
    }
});


test("ordinary static sockets remain pinned to their declared live index", async () => {
    const workflow = existingEdgeWorkflow();
    const targetNode = workflow.nodes.find(node => node.id === 2);
    targetNode.inputs = [
        input("unrelated", "IMAGE", 9, 0),
        input("image_1", "IMAGE", 0, 1),
    ];
    const adapter = new FakeGraphPatchAdapter(workflow);
    const before = adapter.captureWorkflow();
    const request = await existingEdgePatch(adapter, {
        applicationId: "static-socket-index-remains-exact",
    });

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "graph_patch_slot_mismatch");
    assert.deepEqual(adapter.workflow, before);
});


test("a native converter link mismatch rolls back Seedance, CreateVideo, and VHS atomically", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const before = adapter.captureWorkflow();
    adapter.sabotageConnectionTypeAt = 6;
    const request = await nativeVideoConverterBundlePatch(adapter);

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.applied, false);
    assert.equal(result.queued, false);
    assert.equal(result.error.code, "graph_patch_connection_failed");
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, true);
    assert.equal(adapter.createCalls, 5);
    assert.equal(adapter.connectCalls, 6);
    assert.equal(adapter.convertCalls.length, 2);
    assert.equal(adapter.restoreCalls, 1);
    assert.deepEqual(adapter.workflow, before);
});


test("GraphPatch v2 owns a create-only EmptyImage to SaveImage build on an empty canvas", async () => {
    const workflow = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "empty-image-create-only-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "8".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: { nodes: [], edges: [] },
            create_nodes: [
                {
                    alias: "empty_image",
                    node_type: "EmptyImage",
                    schema_hash: SCHEMAS.EmptyImage,
                    values: { width: 512, height: 512, batch_size: 1, color: 0 },
                },
                {
                    alias: "save_image",
                    node_type: "SaveImage",
                    schema_hash: SCHEMAS.SaveImage,
                    values: { filename_prefix: "ren-graph-build" },
                },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: [edge(
                source(newRef("empty_image"), 0, "IMAGE", "IMAGE"),
                target(newRef("save_image"), 0, 0, "images", "IMAGE"),
            )],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 2,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 0,
                final_node_count: 2,
                final_edge_count: 1,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(result.queued, false);
    assert.deepEqual(result.aliases, { empty_image: 1, save_image: 2 });
    assert.equal(adapter.workflow.nodes.length, 2);
    assert.equal(adapter.workflow.links.length, 1);
    assert.equal(adapter.workflow.links[0].source_node_id, 1);
    assert.equal(adapter.workflow.links[0].target_node_id, 2);
    assert.equal(adapter.workflow.nodes[1].values.filename_prefix, "ren-graph-build");
    assert.deepEqual(adapter.events.map(item => item.phase), ["node", "node", "connection"]);
});


test("dynamic fixpoint connects image_1 before an earlier-declared image_2 edge", async () => {
    const workflow = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const addEdges = [
        edge(
            source(newRef("source_two"), 0, "IMAGE", "IMAGE"),
            target(newRef("dynamic_target"), 1, 1, "image_2", "IMAGE"),
        ),
        edge(
            source(newRef("source_one"), 0, "IMAGE", "IMAGE"),
            target(newRef("dynamic_target"), 0, 0, "image_1", "IMAGE"),
        ),
    ];
    const request = {
        application_id: "dynamic-fixpoint-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "e".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: { nodes: [], edges: [] },
            create_nodes: [
                { alias: "source_one", node_type: "Source", schema_hash: SCHEMAS.Source, values: {} },
                { alias: "source_two", node_type: "Source", schema_hash: SCHEMAS.Source, values: {} },
                { alias: "dynamic_target", node_type: "DynamicTarget", schema_hash: SCHEMAS.DynamicTarget, values: {} },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: addEdges,
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 3,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 2,
                removed_edge_count: 0,
                final_node_count: 3,
                final_edge_count: 2,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(
        adapter.workflow.links.map(item => item.target_input),
        ["image_1", "image_2"],
    );
});


test("target resolution honors backend duplicate-name occurrence independently of type", async () => {
    const workflow = {
        version: 0.4,
        last_node_id: 2,
        last_link_id: 0,
        nodes: [
            graphNode({
                id: 1,
                type: "Source",
                schemaHash: SCHEMAS.Source,
                outputs: [output("IMAGE", "IMAGE", 0)],
            }),
            graphNode({
                id: 2,
                type: "DynamicTarget",
                schemaHash: SCHEMAS.DynamicTarget,
                inputs: [input("variant", "IMAGE", 4, 0)],
                schemaInputs: [{
                    name: "variant",
                    type: "IMAGE",
                    index: 4,
                    occurrence_index: 1,
                    kind: "socket",
                }],
            }),
        ],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "duplicate-name-occurrence-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "b".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "DynamicTarget", SCHEMAS.DynamicTarget),
                ],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [],
            add_edges: [edge(
                source(existingRef(1), 0, "IMAGE", "IMAGE"),
                target(existingRef(2), 4, 0, "variant", "IMAGE", "slot", 1),
            )],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 0,
                final_node_count: 2,
                final_edge_count: 1,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(adapter.workflow.links[0].target_input, "variant");
    assert.equal(adapter.workflow.links[0].target_input_index, 0);
});


test("a concrete MATCHTYPE binding uses an adapter-resolved accepted type", async () => {
    const targetNode = graphNode({
        id: 2,
        type: "DynamicTarget",
        schemaHash: SCHEMAS.DynamicTarget,
        inputs: [input("payload", "*", 0, 0)],
        schemaInputs: [schemaInput("payload", "COMFY_MATCHTYPE_V3", 0)],
    });
    targetNode.inputs[0].accepted_types = ["IMAGE", "MASK"];
    targetNode.schema_inputs[0].accepted_types = ["IMAGE", "MASK"];
    const workflow = {
        version: 0.4,
        last_node_id: 2,
        last_link_id: 0,
        nodes: [
            graphNode({
                id: 1,
                type: "Source",
                schemaHash: SCHEMAS.Source,
                outputs: [output("IMAGE", "IMAGE", 0)],
            }),
            targetNode,
        ],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "matchtype-concrete-binding-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "4".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "DynamicTarget", SCHEMAS.DynamicTarget),
                ],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [],
            add_edges: [edge(
                source(existingRef(1), 0, "IMAGE", "IMAGE"),
                target(existingRef(2), 0, 0, "payload", "IMAGE"),
            )],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 0,
                final_node_count: 2,
                final_edge_count: 1,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(adapter.workflow.links[0].type, "IMAGE");
});


test("exact node values use a fixpoint when a selector materializes dotted widgets", async () => {
    const workflow = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "dynamic-value-fixpoint-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "1".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: { nodes: [], edges: [] },
            create_nodes: [{
                alias: "dynamic_values",
                node_type: "DynamicValues",
                schema_hash: SCHEMAS.DynamicValues,
                // Deliberately place the dependent value first. The executor
                // must retry it after selector materializes the dotted widget.
                values: { "selector.detail": 7, selector: "advanced" },
            }],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 1,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 1,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(adapter.workflow.nodes[0].values, {
        selector: "advanced",
        "selector.detail": 7,
    });
});


test("COMFY_DYNAMICSLOT values can materialize only after their activating edge connects", async () => {
    const workflow = {
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "dynamic-slot-edge-value-fixpoint",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "3".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: { nodes: [], edges: [] },
            create_nodes: [
                {
                    alias: "dynamic_slot",
                    node_type: "DynamicSlotValues",
                    schema_hash: SCHEMAS.DynamicSlotValues,
                    values: { "payload.strength": 0.75 },
                },
                {
                    alias: "source",
                    node_type: "Source",
                    schema_hash: SCHEMAS.Source,
                    values: {},
                },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: [edge(
                source(newRef("source"), 0, "IMAGE", "IMAGE"),
                target(newRef("dynamic_slot"), 0, 0, "payload", "IMAGE"),
            )],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 2,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 0,
                final_node_count: 2,
                final_edge_count: 1,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(result.queued, false);
    assert.equal(result.rollback.attempted, false);
    assert.equal(adapter.connectCalls, 1);
    assert.equal(adapter.workflow.links.length, 1);
    const dynamic = adapter.workflow.nodes.find(node => node.type === "DynamicSlotValues");
    assert.equal(dynamic.values["payload.strength"], 0.75);
    assert.deepEqual(
        adapter.events.map(item => item.phase),
        ["node", "node", "connection"],
    );
});


test("an existing selector update may deterministically rematerialize widgets and slots", async () => {
    const dynamic = createdNodeTemplate("DynamicValues", 1);
    const workflow = {
        version: 0.4,
        last_node_id: 1,
        last_link_id: 0,
        nodes: [dynamic],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "existing-dynamic-update-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "6".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [assertion(1, "DynamicValues", SCHEMAS.DynamicValues)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [{
                ref: existingRef(1),
                node_type: "DynamicValues",
                schema_hash: SCHEMAS.DynamicValues,
                expected_values: { selector: "basic" },
                set_values: { selector: "advanced" },
            }],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 1,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 1,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(adapter.workflow.nodes[0].values.selector, "advanced");
    assert.equal(adapter.workflow.nodes[0].values["selector.detail"], 0);
    assert.equal(adapter.workflow.nodes[0].inputs[0].name, "selector.payload");
});


test("an ordinary update cannot delete an unrelated unconnected input or output manifest", async () => {
    for (const manifest of ["input", "output"]) {
        const adapter = new FakeGraphPatchAdapter();
        const before = adapter.captureWorkflow();
        adapter.sabotageManifestOnSet = { node_id: 60, manifest };
        const request = {
            application_id: `ordinary-update-manifest-${manifest}`,
            expected_catalog_hash: CATALOG_HASH,
            patch_hash: manifest === "input" ? "1".repeat(64) : "2".repeat(64),
            plan: {
                operation: "patch",
                expected_workflow_identity: WORKFLOW_IDENTITY,
                expected_graph_hash: await workflowGraphHash(before),
                assertions: {
                    nodes: [assertion(60, "WaveletColorFix", SCHEMAS.WaveletColorFix)],
                    edges: [],
                },
                create_nodes: [],
                update_nodes: [{
                    ref: existingRef(60),
                    node_type: "WaveletColorFix",
                    schema_hash: SCHEMAS.WaveletColorFix,
                    expected_values: { align_method: "wavelet" },
                    set_values: { align_method: "adain" },
                }],
                remove_edges: [],
                add_edges: [],
                remove_nodes: [],
                attachments: [],
                expected_delta: {
                    created_node_count: 0,
                    updated_node_count: 1,
                    removed_node_count: 0,
                    added_edge_count: 0,
                    removed_edge_count: 0,
                    final_node_count: 9,
                    final_edge_count: 13,
                },
            },
        };

        const result = await applyWorkflowGraphPatchAtomic(request, adapter);

        assert.equal(result.success, false, manifest);
        assert.equal(result.error.code, "post_graph_patch_verification_failed", manifest);
        assert.equal(result.rollback.complete, true, manifest);
        assert.deepEqual(adapter.workflow, before, manifest);
        assert.ok(
            result.verification.issues.some(issue => (
                issue.code === "graph_patch_existing_node_changed"
                && issue.manifest === `${manifest}s`
            )),
            JSON.stringify(result.verification.issues),
        );
    }
});


test("a value callback auto-resize is restored to the exact preflight rect", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const beforeNode = structuredClone(adapter.workflow.nodes.find(node => node.id === 60));
    adapter.resizeOnSetNodeId = 60;
    const request = {
        application_id: "update-auto-resize-restoration",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "3".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            assertions: {
                nodes: [assertion(60, "WaveletColorFix", SCHEMAS.WaveletColorFix)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [{
                ref: existingRef(60),
                node_type: "WaveletColorFix",
                schema_hash: SCHEMAS.WaveletColorFix,
                expected_values: { align_method: "wavelet" },
                set_values: { align_method: "adain" },
            }],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 1,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 9,
                final_edge_count: 13,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    const afterNode = adapter.workflow.nodes.find(node => node.id === 60);
    assert.deepEqual(afterNode.pos, beforeNode.pos);
    assert.deepEqual(afterNode.size, beforeNode.size);
    assert.equal(afterNode.values.align_method, "adain");
});


test("a connection callback auto-resize is restored, including an explicit requested rect", async () => {
    for (const explicitLayout of [null, { x: 700, y: -90, width: 410, height: 255 }]) {
        const adapter = new FakeGraphPatchAdapter(existingEdgeWorkflow());
        const beforeTarget = structuredClone(adapter.workflow.nodes[1]);
        adapter.resizeOnConnectTargetId = 2;
        const updateNodes = explicitLayout ? [{
            ref: existingRef(2),
            node_type: "DynamicTarget",
            schema_hash: SCHEMAS.DynamicTarget,
            expected_values: {},
            set_values: {},
            layout_hint: explicitLayout,
        }] : [];
        const request = await existingEdgePatch(adapter, {
            applicationId: explicitLayout
                ? "connect-auto-resize-explicit-layout"
                : "connect-auto-resize-original-layout",
            updateNodes,
        });

        const result = await applyWorkflowGraphPatchAtomic(request, adapter);

        assert.equal(result.success, true, JSON.stringify(result));
        const targetNode = adapter.workflow.nodes.find(node => node.id === 2);
        assert.deepEqual(
            targetNode.pos,
            explicitLayout ? [explicitLayout.x, explicitLayout.y] : beforeTarget.pos,
        );
        assert.deepEqual(
            targetNode.size,
            explicitLayout ? [explicitLayout.width, explicitLayout.height] : beforeTarget.size,
        );
    }
});


test("callback damage to an unrelated node rect still rolls back the full patch", async () => {
    const adapter = new FakeGraphPatchAdapter(existingEdgeWorkflow({ withUnrelated: true }));
    const before = adapter.captureWorkflow();
    adapter.damageUnrelatedRectOnConnectNodeId = 3;
    const request = await existingEdgePatch(adapter, {
        applicationId: "unrelated-rect-damage-rollback",
        finalNodeCount: 3,
    });

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "post_graph_patch_verification_failed");
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(adapter.workflow, before);
    assert.ok(result.verification.issues.some(issue => (
        issue.code === "graph_patch_existing_node_changed" && issue.node_id === "3"
    )));
});


test("an existing-node attachment is applied and verified as an intentional value change", async () => {
    const attached = createdNodeTemplate("AttachmentNode", 1);
    const workflow = {
        version: 0.4,
        last_node_id: 1,
        last_link_id: 0,
        nodes: [attached],
        links: [],
        groups: [],
        config: {},
        extra: { preserve: true },
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "existing-attachment-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "2".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [assertion(1, "AttachmentNode", SCHEMAS.AttachmentNode)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [{
                ref: existingRef(1),
                input_index: 0,
                input: "image",
                type: "STRING",
                filename: "subject.png",
                subfolder: "ren-chat",
                file_type: "input",
                size_bytes: 123,
                sha256: ATTACHMENT_SHA256,
            }],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 1,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(adapter.workflow.nodes[0].values.image, {
        filename: "subject.png",
        subfolder: "ren-chat",
        type: "input",
    });
    assert.equal(adapter.workflow.extra.preserve, true);
    assert.equal(adapter.attachmentCalls[0].size_bytes, 123);
    assert.equal(adapter.attachmentCalls[0].sha256, ATTACHMENT_SHA256);
});


test("a forged v2 ledger cannot hide an attachment postcondition mismatch", async () => {
    const attached = createdNodeTemplate("AttachmentNode", 1);
    const workflow = {
        version: 0.4,
        last_node_id: 1,
        last_link_id: 0,
        nodes: [attached],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "forged-attachment-retry-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "a".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [assertion(1, "AttachmentNode", SCHEMAS.AttachmentNode)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [{
                ref: existingRef(1),
                input_index: 0,
                input: "image",
                type: "STRING",
                filename: "subject.png",
                subfolder: "ren-chat",
                file_type: "input",
                size_bytes: 123,
                sha256: ATTACHMENT_SHA256,
            }],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 1,
                final_edge_count: 0,
            },
        },
    };
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);
    const wrong = { filename: "wrong.png", subfolder: "ren-chat", type: "input" };
    adapter.workflow.nodes[0].values.image = structuredClone(wrong);
    adapter.workflow.nodes[0].widgets.find(item => item.name === "image").value = structuredClone(wrong);
    adapter.workflow.nodes[0].widgets_values = Object.values(
        structuredClone(adapter.workflow.nodes[0].values),
    );
    const content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY]
        .entries[request.application_id].result_content_hash = await workflowGraphHash(content);

    const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_attachment_mismatch"
    )));
});


test("attachment normalization rejects non-attested and non-Ren image references", async () => {
    const variants = [
        { filename: "nested/subject.png" },
        { filename: "/absolute.png" },
        { subfolder: "output" },
        { subfolder: "ren-chat/../other" },
        { file_type: "output" },
        { file_type: "temp" },
        { size_bytes: 0 },
        { sha256: "not-a-digest" },
    ];
    for (const variant of variants) {
        const attached = createdNodeTemplate("AttachmentNode", 1);
        const workflow = {
            version: 0.4,
            last_node_id: 1,
            last_link_id: 0,
            nodes: [attached],
            links: [],
            groups: [],
            config: {},
            extra: {},
        };
        const adapter = new FakeGraphPatchAdapter(workflow);
        const attachment = {
            ref: existingRef(1),
            input_index: 0,
            input: "image",
            type: "STRING",
            filename: "subject.png",
            subfolder: "ren-chat/session",
            file_type: "input",
            size_bytes: 123,
            sha256: ATTACHMENT_SHA256,
            ...variant,
        };
        const request = {
            application_id: "invalid-attachment-test",
            expected_catalog_hash: CATALOG_HASH,
            patch_hash: PATCH_HASH,
            plan: {
                operation: "patch",
                expected_workflow_identity: WORKFLOW_IDENTITY,
                expected_graph_hash: await workflowGraphHash(workflow),
                assertions: {
                    nodes: [assertion(1, "AttachmentNode", SCHEMAS.AttachmentNode)],
                    edges: [],
                },
                create_nodes: [],
                update_nodes: [],
                remove_edges: [],
                add_edges: [],
                remove_nodes: [],
                attachments: [attachment],
                expected_delta: {
                    created_node_count: 0,
                    updated_node_count: 0,
                    removed_node_count: 0,
                    added_edge_count: 0,
                    removed_edge_count: 0,
                    final_node_count: 1,
                    final_edge_count: 0,
                },
            },
        };

        const result = await applyWorkflowGraphPatchAtomic(request, adapter);
        assert.equal(result.success, false, JSON.stringify(variant));
        assert.equal(result.error.code, "invalid_graph_patch", JSON.stringify(variant));
        assert.equal(adapter.attachmentCalls.length, 0);
    }
});


test("attachment normalization bounds one chat request to eight images", async () => {
    const attached = createdNodeTemplate("AttachmentNode", 1);
    const workflow = {
        version: 0.4,
        last_node_id: 1,
        last_link_id: 0,
        nodes: [attached],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const attachment = {
        ref: existingRef(1),
        input_index: 0,
        input: "image",
        type: "STRING",
        filename: "subject.png",
        subfolder: "ren-chat/session",
        file_type: "input",
        size_bytes: 123,
        sha256: ATTACHMENT_SHA256,
    };
    const request = {
        application_id: "too-many-attachments-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: PATCH_HASH,
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [assertion(1, "AttachmentNode", SCHEMAS.AttachmentNode)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: Array.from({ length: 9 }, () => ({ ...attachment })),
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 1,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "invalid_graph_patch");
    assert.equal(adapter.attachmentCalls.length, 0);
});


test("one existing node can receive an exact update and attachment atomically", async () => {
    const attached = createdNodeTemplate("AttachmentNode", 1);
    const workflow = {
        version: 0.4,
        last_node_id: 1,
        last_link_id: 0,
        nodes: [attached],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "update-and-attachment-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "5".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [assertion(1, "AttachmentNode", SCHEMAS.AttachmentNode)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [{
                ref: existingRef(1),
                node_type: "AttachmentNode",
                schema_hash: SCHEMAS.AttachmentNode,
                expected_values: { quality: "draft" },
                set_values: { quality: "final" },
            }],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [{
                ref: existingRef(1),
                input_index: 0,
                input: "image",
                type: "STRING",
                filename: "approved.png",
                subfolder: "ren-chat",
                file_type: "input",
                size_bytes: 456,
                sha256: ATTACHMENT_SHA256,
            }],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 1,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 1,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(adapter.workflow.nodes[0].values.quality, "final");
    assert.equal(adapter.verifyAttachmentExact(1, request.plan.attachments[0]), true);
});


test("arbitrary remove/add edge delta is exact and leaves all nodes untouched", async () => {
    const nodes = [
        graphNode({ id: 1, type: "Source", schemaHash: SCHEMAS.Source, outputs: [output("IMAGE", "IMAGE", 0)] }),
        graphNode({ id: 2, type: "DynamicTarget", schemaHash: SCHEMAS.DynamicTarget, inputs: [input("image_1", "IMAGE", 0, 0)] }),
        graphNode({ id: 3, type: "DynamicTarget", schemaHash: SCHEMAS.DynamicTarget, inputs: [input("image_1", "IMAGE", 0, 0)] }),
    ];
    const oldEdge = link(1, 1, 0, "IMAGE", 2, 0, "image_1", "IMAGE");
    const workflow = {
        version: 0.4,
        last_node_id: 3,
        last_link_id: 1,
        nodes,
        links: [oldEdge],
        groups: [],
        config: {},
        extra: { project: "preserve" },
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const oldCanonical = edge(
        source(existingRef(1), 0, "IMAGE", "IMAGE"),
        target(existingRef(2), 0, 0, "image_1", "IMAGE"),
    );
    const newCanonical = edge(
        source(existingRef(1), 0, "IMAGE", "IMAGE"),
        target(existingRef(3), 0, 0, "image_1", "IMAGE"),
    );
    const request = {
        application_id: "remove-add-edge-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "f".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "DynamicTarget", SCHEMAS.DynamicTarget),
                    assertion(3, "DynamicTarget", SCHEMAS.DynamicTarget),
                ],
                edges: [oldCanonical],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [oldCanonical],
            add_edges: [newCanonical],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 1,
                removed_edge_count: 1,
                final_node_count: 3,
                final_edge_count: 1,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.equal(adapter.disconnectCalls, 1);
    assert.deepEqual(adapter.workflow.nodes, nodes);
    assert.equal(adapter.workflow.links.length, 1);
    assert.equal(adapter.workflow.links[0].target_node_id, 3);
    assert.equal(adapter.workflow.extra.project, "preserve");
});


test("multiple dynamic removals disconnect highest sockets first before compaction", async () => {
    const nodes = [
        graphNode({ id: 1, type: "Source", schemaHash: SCHEMAS.Source, outputs: [output("IMAGE", "IMAGE", 0)] }),
        graphNode({ id: 2, type: "Source", schemaHash: SCHEMAS.Source, outputs: [output("IMAGE", "IMAGE", 0)] }),
        graphNode({
            id: 3,
            type: "DynamicTarget",
            schemaHash: SCHEMAS.DynamicTarget,
            inputs: [
                input("image_1", "IMAGE", 0, 0),
                input("image_2", "IMAGE", 1, 1),
            ],
            schemaInputs: [
                schemaInput("image_1", "IMAGE", 0),
                schemaInput("image_2", "IMAGE", 1),
            ],
        }),
    ];
    const first = edge(
        source(existingRef(1), 0, "IMAGE", "IMAGE"),
        target(existingRef(3), 0, 0, "image_1", "IMAGE"),
    );
    const second = edge(
        source(existingRef(2), 0, "IMAGE", "IMAGE"),
        target(existingRef(3), 1, 1, "image_2", "IMAGE"),
    );
    const workflow = {
        version: 0.4,
        last_node_id: 3,
        last_link_id: 2,
        nodes,
        links: [
            link(1, 1, 0, "IMAGE", 3, 0, "image_1", "IMAGE"),
            link(2, 2, 0, "IMAGE", 3, 1, "image_2", "IMAGE"),
        ],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    adapter.compactInputsOnDisconnect = true;
    const request = {
        application_id: "dynamic-remove-compaction-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "3".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "Source", SCHEMAS.Source),
                    assertion(3, "DynamicTarget", SCHEMAS.DynamicTarget),
                ],
                edges: [first, second],
            },
            create_nodes: [],
            update_nodes: [],
            remove_edges: [first, second],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 2,
                final_node_count: 3,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, true);
    assert.deepEqual(
        adapter.disconnectHistory.map(item => item.target_input_index),
        [1, 0],
    );
    assert.equal(adapter.workflow.links.length, 0);
    assert.equal(adapter.workflow.nodes[2].inputs.length, 0);
});


test("numeric and string node-ID collisions fail closed before any mutation", async () => {
    const workflow = {
        version: 0.4,
        last_node_id: 2,
        last_link_id: 0,
        nodes: [
            createdNodeTemplate("Source", 2),
            createdNodeTemplate("Source", "2"),
        ],
        links: [],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "ambiguous-node-identity-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "4".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: { nodes: [], edges: [] },
            create_nodes: [{
                alias: "new_image",
                node_type: "EmptyImage",
                schema_hash: SCHEMAS.EmptyImage,
                values: {},
            }],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 1,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 3,
                final_edge_count: 0,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "ambiguous_node_identity");
    assert.equal(adapter.createCalls, 0);
    assert.equal(adapter.connectCalls, 0);
    assert.equal(adapter.restoreCalls, 0);
    assert.equal(result.rollback.attempted, false);
    assert.deepEqual(adapter.captureWorkflow(), workflow);
});


test("a final-graph cycle fails before any canvas mutation", async () => {
    const adapter = new FakeGraphPatchAdapter({
        version: 0.4,
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
    });
    const request = {
        application_id: "cycle-preflight-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "0".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(adapter.captureWorkflow()),
            assertions: { nodes: [], edges: [] },
            create_nodes: [
                { alias: "first", node_type: "Source", schema_hash: SCHEMAS.Source, values: {} },
                { alias: "second", node_type: "DynamicTarget", schema_hash: SCHEMAS.DynamicTarget, values: {} },
            ],
            update_nodes: [],
            remove_edges: [],
            add_edges: [
                edge(
                    source(newRef("first"), 0, "IMAGE", "IMAGE"),
                    target(newRef("second"), 0, 0, "image_1", "IMAGE"),
                ),
                edge(
                    source(newRef("second"), 0, "IMAGE", "IMAGE"),
                    target(newRef("first"), 0, 0, "image_1", "IMAGE"),
                ),
            ],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 2,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 2,
                removed_edge_count: 0,
                final_node_count: 2,
                final_edge_count: 2,
            },
        },
    };

    const result = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "graph_patch_cycle");
    assert.equal(adapter.createCalls, 0);
    assert.equal(result.rollback.attempted, false);
});


test("a mid-DAG connection failure restores the exact 9/13 snapshot and hash", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const before = adapter.captureWorkflow();
    const beforeHash = await workflowGraphHash(before);
    adapter.failConnectAt = 4;

    const result = await applyWorkflowGraphPatchAtomic(await productionPatch(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.rollback.attempted, true);
    assert.equal(result.rollback.complete, true);
    assert.equal(result.rollback.hash_verified, true);
    assert.equal(adapter.restoreCalls, 1);
    assert.deepEqual(adapter.captureWorkflow(), before);
    assert.equal(await workflowGraphHash(adapter.captureWorkflow()), beforeHash);
});


test("a collateral serialized widgets_values change is detected and rolled back", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const before = adapter.captureWorkflow();
    adapter.sabotageUnrelatedWidgetAfterConnect = true;

    const result = await applyWorkflowGraphPatchAtomic(await productionPatch(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "post_graph_patch_verification_failed");
    assert.ok(result.error.details.issues.some(issue => (
        issue.code === "graph_patch_existing_node_changed"
        && issue.node_id === "48"
    )));
    assert.equal(result.rollback.complete, true);
    assert.deepEqual(adapter.captureWorkflow(), before);
});


test("an unresolved widget fails after bounded dependency scheduling and rolls back exactly", async () => {
    const adapter = new FakeGraphPatchAdapter();
    adapter.skipValue = "model.prompt";

    const result = await applyWorkflowGraphPatchAtomic(await productionPatch(adapter), adapter);

    assert.equal(result.success, false);
    assert.equal(result.error.code, "graph_patch_value_application_failed");
    assert.equal(adapter.connectCalls, 6);
    assert.equal(result.rollback.complete, true);
    assert.equal(adapter.workflow.nodes.length, 9);
    assert.equal(adapter.workflow.links.length, 13);
});


test("retrying the identical patch is idempotent without duplicate DAG nodes or edges", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const request = await productionPatch(adapter);
    const first = await applyWorkflowGraphPatchAtomic(request, adapter);
    const nodesAfterFirst = adapter.workflow.nodes.length;
    const edgesAfterFirst = adapter.workflow.links.length;
    const createCalls = adapter.createCalls;

    const second = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(first.success, true);
    assert.equal(second.success, true);
    assert.equal(second.applied, false);
    assert.equal(second.already_applied, true);
    assert.equal(adapter.workflow.nodes.length, nodesAfterFirst);
    assert.equal(adapter.workflow.links.length, edgesAfterFirst);
    assert.equal(adapter.createCalls, createCalls);
});


test("a forged retry ledger cannot bind a converted widget edge to the wrong duplicate occurrence", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const request = await productionPatch(adapter);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);

    const ledger = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const entry = ledger.entries[request.application_id];
    const combineId = entry.aliases.video_combine;
    const combine = adapter.workflow.nodes.find(node => sameId(node.id, combineId));
    const converted = combine.inputs.find(inputValue => inputValue.name === "frame_rate");
    const convertedEdge = adapter.workflow.links.find(edgeValue => (
        sameId(edgeValue.target_node_id, combineId)
        && edgeValue.target_input === "frame_rate"
    ));
    assert.ok(converted);
    assert.ok(convertedEdge);

    converted.occurrence_index = 0;
    const wrongOccurrence = {
        ...structuredClone(converted),
        socket_index: combine.inputs.length,
        occurrence_index: 1,
        link: null,
    };
    combine.inputs.push(wrongOccurrence);
    convertedEdge.target_input_index = wrongOccurrence.socket_index;

    const content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);

    const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_edge_mismatch"
    )));
});


test("a converted retry accepts one unlabelled target but rejects an unlabelled duplicate", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const request = await productionPatch(adapter);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);

    const ledger = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const entry = ledger.entries[request.application_id];
    const combineId = entry.aliases.video_combine;
    const combine = adapter.workflow.nodes.find(node => sameId(node.id, combineId));
    const converted = combine.inputs.find(inputValue => inputValue.name === "frame_rate");
    assert.ok(converted);
    delete converted.schema_index;
    delete converted.input_index;
    delete converted.occurrence_index;

    let content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);
    const uniqueRetry = await applyWorkflowGraphPatchAtomic(request, adapter);
    assert.equal(uniqueRetry.success, true);
    assert.equal(uniqueRetry.already_applied, true);

    combine.inputs.push({
        name: converted.name,
        type: converted.type,
        socket_index: combine.inputs.length,
        link: null,
    });
    content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);

    const ambiguousRetry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(ambiguousRetry.success, false);
    assert.equal(ambiguousRetry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(ambiguousRetry.error.details.issues.some(issue => (
        issue.code === "graph_patch_edge_mismatch"
    )));
});


test("a forged root retry ledger cannot hide retained asserted node or edge substitution", async () => {
    const workflow = existingEdgeWorkflow({ withUnrelated: true });
    workflow.last_link_id = 1;
    workflow.links = [link(1, 1, 0, "IMAGE", 2, 0, "image_1", "IMAGE")];
    const original = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "retained-assertion-retry-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "4".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "DynamicTarget", SCHEMAS.DynamicTarget),
                    assertion(3, "Source", SCHEMAS.Source),
                ],
                edges: [edge(
                    source(existingRef(1), 0, "IMAGE", "IMAGE"),
                    target(existingRef(2), 0, 0, "image_1", "IMAGE"),
                )],
            },
            create_nodes: [{
                alias: "isolated_source",
                node_type: "Source",
                schema_hash: SCHEMAS.Source,
                values: {},
            }],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 1,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 4,
                final_edge_count: 1,
            },
        },
    };
    assert.equal((await applyWorkflowGraphPatchAtomic(request, original)).success, true);
    const baseline = original.captureWorkflow();
    const corruptions = [
        {
            code: "graph_patch_asserted_node_mismatch",
            apply(adapter) {
                adapter.workflow.nodes.find(node => sameId(node.id, 3)).schema_hash = "f".repeat(64);
            },
        },
        {
            code: "graph_patch_asserted_edge_mismatch",
            apply(adapter) {
                adapter.workflow.links[0].source_node_id = 3;
            },
        },
    ];

    for (const corruption of corruptions) {
        const adapter = new FakeGraphPatchAdapter(baseline);
        corruption.apply(adapter);
        const ledger = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
        const content = adapter.captureWorkflow();
        delete content.extra[GRAPH_PATCH_LEDGER_KEY];
        ledger.entries[request.application_id].result_content_hash = await workflowGraphHash(content);

        const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

        assert.equal(retry.success, false);
        assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
        assert.ok(retry.error.details.issues.some(issue => issue.code === corruption.code));
    }
});


test("a forged root retry ledger cannot attach an undeclared edge to a created node", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const request = await productionPatch(adapter);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);

    const ledger = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const componentsId = ledger.entries[request.application_id].aliases.video_components;
    const displaced = adapter.workflow.links.find(edgeValue => edgeValue.id === 45);
    assert.ok(displaced);
    displaced.source_node_id = componentsId;
    displaced.source_output_index = 0;
    displaced.source_output = "images";
    const content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    ledger.entries[request.application_id].result_content_hash = await workflowGraphHash(content);

    const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_created_incident_edge_mismatch"
        && issue.alias === "video_components"
    )));
});


test("created-node retry edge proof preserves exact typed endpoint IDs", async () => {
    const adapter = new FakeGraphPatchAdapter();
    const request = await productionPatch(adapter);
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);

    const ledger = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const entry = ledger.entries[request.application_id];
    const componentsId = entry.aliases.video_components;
    const combineId = entry.aliases.video_combine;
    const typedEdge = adapter.workflow.links.find(edgeValue => (
        sameId(edgeValue.source_node_id, componentsId)
        && sameId(edgeValue.target_node_id, combineId)
        && edgeValue.target_input === "images"
    ));
    assert.ok(typedEdge);
    typedEdge.source_node_id = String(componentsId);
    assert.notEqual(typeof typedEdge.source_node_id, typeof componentsId);
    const content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    entry.result_content_hash = await workflowGraphHash(content);

    const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_edge_mismatch"
    )));
});


test("root retry rejects a string node and edge substituted for numeric plan IDs", async () => {
    const adapter = new FakeGraphPatchAdapter(existingEdgeWorkflow());
    const request = await existingEdgePatch(adapter, {
        applicationId: "typed-root-substitution-retry-test",
    });
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);

    const numericNode = adapter.workflow.nodes.find(node => (
        typeof node.id === "number" && node.id === 1
    ));
    const numericEdge = adapter.workflow.links[0];
    numericNode.id = "1";
    numericEdge.source_node_id = "1";
    const ledger = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY];
    const content = adapter.captureWorkflow();
    delete content.extra[GRAPH_PATCH_LEDGER_KEY];
    ledger.entries[request.application_id].result_content_hash = await workflowGraphHash(content);

    const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_asserted_node_mismatch"
    )));
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_edge_mismatch"
    )));
});


test("a substantive root ledger-only import cannot claim an unchanged pre-patch graph", async () => {
    const workflow = existingEdgeWorkflow();
    const authority = new FakeGraphPatchAdapter(workflow);
    const request = await existingEdgePatch(authority, {
        applicationId: "unchanged-root-ledger-test",
    });
    assert.equal((await applyWorkflowGraphPatchAtomic(request, authority)).success, true);
    const forged = new FakeGraphPatchAdapter(workflow);
    forged.workflow.extra[GRAPH_PATCH_LEDGER_KEY] = structuredClone(
        authority.workflow.extra[GRAPH_PATCH_LEDGER_KEY],
    );
    forged.workflow.extra[GRAPH_PATCH_LEDGER_KEY]
        .entries[request.application_id].result_content_hash = request.plan.expected_graph_hash;

    const retry = await applyWorkflowGraphPatchAtomic(request, forged);

    assert.equal(retry.success, false);
    assert.equal(retry.error.code, "graph_patch_idempotency_conflict");
    assert.ok(retry.error.details.issues.some(issue => (
        issue.code === "graph_patch_delta_mismatch"
        || issue.code === "graph_patch_edge_mismatch"
    )));
    assert.equal(forged.createCalls, 0);
    assert.equal(forged.connectCalls, 0);
});


test("a same-value and same-layout root update retries idempotently", async () => {
    const workflow = productionWorkflow();
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "root-no-op-update-retry-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "5".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [assertion(60, "WaveletColorFix", SCHEMAS.WaveletColorFix)],
                edges: [],
            },
            create_nodes: [],
            update_nodes: [{
                ref: existingRef(60),
                node_type: "WaveletColorFix",
                schema_hash: SCHEMAS.WaveletColorFix,
                expected_values: { align_method: "wavelet" },
                set_values: { align_method: "wavelet" },
                layout_hint: { x: 1800, y: 0, width: 240, height: 140 },
            }],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 0,
                updated_node_count: 1,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 9,
                final_edge_count: 13,
            },
        },
    };

    const first = await applyWorkflowGraphPatchAtomic(request, adapter);
    assert.equal(first.success, true, JSON.stringify(first.error));
    const entry = adapter.workflow.extra[GRAPH_PATCH_LEDGER_KEY].entries[request.application_id];
    const second = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(entry.result_content_hash, request.plan.expected_graph_hash);
    assert.equal(second.success, true, JSON.stringify(second.error));
    assert.equal(second.already_applied, true);
});


test("retry assertion proof preserves a trusted dynamic socket relocation", async () => {
    const dynamicInput = "model.reference_images.image_1";
    const workflow = {
        version: 0.4,
        last_node_id: 2,
        last_link_id: 1,
        nodes: [
            graphNode({
                id: 1,
                type: "Source",
                schemaHash: SCHEMAS.Source,
                outputs: [output("IMAGE", "IMAGE", 0)],
            }),
            graphNode({
                id: 2,
                type: "ByteDance2ReferenceNode",
                schemaHash: SCHEMAS.ByteDance2ReferenceNode,
                inputs: [input(dynamicInput, "IMAGE", 6, 5)],
                schemaInputs: [schemaInput(dynamicInput, "IMAGE", 6)],
                dynamicInputRoots: ["model"],
            }),
        ],
        links: [link(1, 1, 0, "IMAGE", 2, 5, dynamicInput, "IMAGE")],
        groups: [],
        config: {},
        extra: {},
    };
    const adapter = new FakeGraphPatchAdapter(workflow);
    const request = {
        application_id: "dynamic-assertion-retry-test",
        expected_catalog_hash: CATALOG_HASH,
        patch_hash: "6".repeat(64),
        plan: {
            operation: "patch",
            expected_workflow_identity: WORKFLOW_IDENTITY,
            expected_graph_hash: await workflowGraphHash(workflow),
            assertions: {
                nodes: [
                    assertion(1, "Source", SCHEMAS.Source),
                    assertion(2, "ByteDance2ReferenceNode", SCHEMAS.ByteDance2ReferenceNode),
                ],
                edges: [edge(
                    source(existingRef(1), 0, "IMAGE", "IMAGE"),
                    target(existingRef(2), 6, 0, dynamicInput, "IMAGE"),
                )],
            },
            create_nodes: [{
                alias: "isolated_source",
                node_type: "Source",
                schema_hash: SCHEMAS.Source,
                values: {},
            }],
            update_nodes: [],
            remove_edges: [],
            add_edges: [],
            remove_nodes: [],
            attachments: [],
            expected_delta: {
                created_node_count: 1,
                updated_node_count: 0,
                removed_node_count: 0,
                added_edge_count: 0,
                removed_edge_count: 0,
                final_node_count: 3,
                final_edge_count: 1,
            },
        },
    };
    assert.equal((await applyWorkflowGraphPatchAtomic(request, adapter)).success, true);

    const retry = await applyWorkflowGraphPatchAtomic(request, adapter);

    assert.equal(retry.success, true, JSON.stringify(retry.error));
    assert.equal(retry.already_applied, true);
});
