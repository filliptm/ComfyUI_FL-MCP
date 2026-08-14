import pytest
from mask_targeting import (
    MaskContextTokenStore,
    resolve_connected_prompt_producer,
    resolve_direct_prompt_widget,
    resolve_mask_target,
    resolve_reference_image_producer,
    resolve_reference_prompt_producer,
)


def nano_direct_prompt_graph(node_id=8):
    return {
        "nodes": [
            {
                "id": 33,
                "type": "LoadImage",
                "inputs": [],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
            },
            {
                "id": node_id,
                "type": "GeminiNanoBanana2V2",
                "inputs": [
                    {
                        "name": "prompt",
                        "type": "STRING",
                        "widget": {"name": "prompt"},
                        "link": None,
                    },
                    {
                        "name": "system_prompt",
                        "type": "STRING",
                        "widget": {"name": "system_prompt"},
                        "link": None,
                    },
                    {"name": "image_2", "type": "IMAGE", "link": 1},
                ],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
            },
        ],
        "links": [[1, 33, 0, node_id, 2, "IMAGE"]],
    }


def recorded_inpaint_graph():
    return {
        "nodes": [
            {
                "id": 1,
                "type": "LoadImage",
                "title": "Inpaint source",
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [1]},
                    {"name": "MASK", "type": "MASK", "links": [2]},
                ],
            },
            {
                "id": 10,
                "type": "InpaintModelConditioning",
                "inputs": [
                    {"name": "image", "type": "IMAGE", "link": 1},
                    {"name": "mask", "type": "MASK", "link": 2},
                    {"name": "prompt", "type": "STRING", "link": 3},
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING", "links": []}],
            },
            {
                "id": 33,
                "type": "LoadImage",
                "title": "Disconnected reference",
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": []},
                    {"name": "MASK", "type": "MASK", "links": []},
                ],
            },
            {
                "id": 34,
                "type": "PrimitiveStringMultiline",
                "title": "Inpaint prompt",
                "inputs": [],
                "outputs": [{"name": "STRING", "type": "STRING", "links": [3]}],
            },
        ],
        "links": [
            [1, 1, 0, 10, 0, "IMAGE"],
            [2, 1, 1, 10, 1, "MASK"],
            [3, 34, 0, 10, 2, "STRING"],
        ],
    }


def test_topology_selects_connected_image_and_mask_source_not_disconnected_node():
    resolution = resolve_mask_target(recorded_inpaint_graph())

    assert resolution.needs_choice is False
    assert resolution.reason == "unique_topology_mask_source"
    assert resolution.target is not None
    assert resolution.target.node_id == 1
    assert resolution.target.connected_output_types == ("IMAGE", "MASK")
    assert resolution.prompt_producers == (
        {
            "producer_node_id": 34,
            "producer_node_type": "PrimitiveStringMultiline",
            "producer_output": "STRING",
            "producer_output_index": 0,
            "consumer_node_id": 10,
            "consumer_node_type": "InpaintModelConditioning",
            "consumer_input": "prompt",
            "consumer_input_index": 2,
        },
    )


def test_explicit_id_and_single_selected_node_are_exact_and_deterministic():
    graph = recorded_inpaint_graph()

    explicit = resolve_mask_target(graph, requested_node_id=33)
    selected = resolve_mask_target(graph, selected_node_ids=[33])
    typed_mismatch = resolve_mask_target(graph, requested_node_id="33")

    assert explicit.target is not None and explicit.target.node_id == 33
    assert explicit.reason == "explicit_exact_id"
    assert selected.target is not None and selected.target.node_id == 33
    assert selected.reason == "single_selected_mask_source"
    assert typed_mismatch.needs_choice is True
    assert typed_mismatch.reason == "requested_node_not_mask_compatible"


def test_multiple_selected_mask_sources_return_bounded_choices_without_guessing():
    resolution = resolve_mask_target(
        recorded_inpaint_graph(),
        selected_node_ids=[1, 33],
    )

    assert resolution.needs_choice is True
    assert resolution.reason == "multiple_selected_mask_sources"
    assert [candidate.node_id for candidate in resolution.candidates] == [1, 33]


def test_connected_prompt_producer_resolves_exact_string_edge():
    resolution = resolve_connected_prompt_producer(recorded_inpaint_graph())

    assert resolution.needs_choice is False
    assert resolution.target is not None
    assert resolution.target.producer_node_id == 34
    assert resolution.target.consumer_node_id == 10
    assert resolution.target.consumer_input == "prompt"


def test_direct_prompt_widget_resolves_exact_serialized_widget_name():
    graph = nano_direct_prompt_graph()
    graph["nodes"][1]["inputs"][0]["widget"]["name"] = "actual_prompt_widget"

    resolution = resolve_direct_prompt_widget(graph, consumer_node_id=8)

    assert resolution.needs_choice is False
    assert resolution.reason == "unique_direct_prompt_widget"
    assert resolution.target is not None
    assert resolution.target.node_id == 8
    assert resolution.target.consumer_input == "prompt"
    assert resolution.target.producer_widget == "actual_prompt_widget"


def test_direct_prompt_widget_explicit_system_prompt_is_exact():
    resolution = resolve_direct_prompt_widget(
        nano_direct_prompt_graph(),
        consumer_node_id=8,
        consumer_input="system_prompt",
    )

    assert resolution.target is not None
    assert resolution.target.consumer_input == "system_prompt"
    assert resolution.target.producer_widget == "system_prompt"

    near_match = resolve_direct_prompt_widget(
        nano_direct_prompt_graph(),
        consumer_node_id=8,
        consumer_input="system prompt",
    )
    assert near_match.target is None
    assert near_match.reason == "no_unconnected_prompt_widget"


def test_direct_prompt_widget_requires_node_choice_across_consumers():
    graph = nano_direct_prompt_graph()
    second = nano_direct_prompt_graph(node_id="8")
    graph["nodes"].append(second["nodes"][1])
    graph["links"].append([2, 33, 0, "8", 2, "IMAGE"])
    graph["nodes"][0]["outputs"][0]["links"].append(2)

    resolution = resolve_direct_prompt_widget(graph)

    assert resolution.needs_choice is True
    assert resolution.reason == "ambiguous_direct_prompt_widgets"
    assert {type(item.node_id) for item in resolution.candidates} == {int, str}
    explicit = resolve_direct_prompt_widget(graph, consumer_node_id="8")
    assert explicit.target is not None and explicit.target.node_id == "8"


def test_direct_prompt_widget_invalid_links_fail_closed():
    graph = nano_direct_prompt_graph()
    graph["links"] = [[1, 999, 0, 8, 2, "IMAGE"]]

    resolution = resolve_direct_prompt_widget(graph, consumer_node_id=8)

    assert resolution.needs_choice is True
    assert resolution.reason == "invalid_workflow_links"
    assert resolution.candidates == ()


def test_reference_route_resolves_direct_nano_prompt_widget():
    graph = nano_direct_prompt_graph()
    reference = resolve_reference_image_producer(graph)
    assert reference.target is not None

    prompt = resolve_reference_prompt_producer(graph, reference.target)

    assert prompt.target is not None
    assert prompt.reason == "unique_reference_route_direct_prompt_widget"
    assert prompt.target.producer_node_id == 8
    assert prompt.target.producer_widget == "prompt"


def test_reference_route_never_hides_a_second_direct_prompt_consumer():
    graph = nano_direct_prompt_graph()
    graph["nodes"][1]["outputs"][0]["links"] = [2]
    graph["nodes"].append(
        {
            "id": 9,
            "type": "DownstreamImageEditor",
            "inputs": [
                {"name": "image", "type": "IMAGE", "link": 2},
                {
                    "name": "negative_prompt",
                    "type": "STRING",
                    "widget": {"name": "negative_prompt"},
                    "link": None,
                },
            ],
            "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
        }
    )
    graph["links"].append([2, 8, 0, 9, 0, "IMAGE"])
    reference = resolve_reference_image_producer(graph)
    assert reference.target is not None

    prompt = resolve_reference_prompt_producer(graph, reference.target)

    assert prompt.target is None
    assert prompt.reason == "ambiguous_reference_route_direct_prompt_widgets"
    assert {item.node_id for item in prompt.candidates} == {8, 9}


def test_reference_role_resolves_image_producer_by_socket_not_fake_node_id():
    graph = recorded_inpaint_graph()
    graph["nodes"][1]["inputs"].append(
        {"name": "image_2", "type": "IMAGE", "link": 4}
    )
    graph["nodes"][2]["outputs"][0]["links"] = [4]
    graph["links"].append([4, 33, 0, 10, 3, "IMAGE"])

    resolution = resolve_reference_image_producer(graph)

    assert resolution.needs_choice is False
    assert resolution.target is not None
    assert resolution.target.producer_node_id == 33
    assert resolution.target.consumer_node_id == 10
    assert resolution.target.consumer_input == "image_2"


def face_swap_resize_graph():
    return {
        "nodes": [
            {
                "id": 1,
                "type": "LoadImage",
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [10]},
                    {"name": "MASK", "type": "MASK", "links": [11]},
                ],
            },
            {
                "id": 3,
                "type": "InpaintCropImproved",
                "inputs": [
                    {"name": "image", "type": "IMAGE", "link": 10},
                    {"name": "mask", "type": "MASK", "link": 11},
                ],
                "outputs": [{"name": "cropped_image", "type": "IMAGE", "links": [12]}],
            },
            {
                "id": 10,
                "type": "LoadImage",
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [37]},
                    {"name": "MASK", "type": "MASK", "links": []},
                ],
            },
            {
                "id": 13,
                "type": "ImageResizeKJv2",
                "inputs": [{"name": "image", "type": "IMAGE", "link": 37}],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [16]}],
            },
            {
                "id": 11,
                "type": "ImageBatchMulti",
                "inputs": [
                    {"name": "image_1", "type": "IMAGE", "link": 12},
                    {"name": "image_2", "type": "IMAGE", "link": 16},
                ],
                "outputs": [{"name": "images", "type": "IMAGE", "links": [17]}],
            },
            {
                "id": 2,
                "type": "GeminiImage2Node",
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": 17},
                    {"name": "prompt", "type": "STRING", "link": 18},
                ],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}],
            },
            {
                "id": 34,
                "type": "PrimitiveStringMultiline",
                "outputs": [{"name": "STRING", "type": "STRING", "links": [18]}],
            },
        ],
        "links": [
            [10, 1, 0, 3, 0, "IMAGE"],
            [11, 1, 1, 3, 1, "MASK"],
            [12, 3, 0, 11, 0, "IMAGE"],
            [37, 10, 0, 13, 0, "IMAGE"],
            [16, 13, 0, 11, 1, "IMAGE"],
            [17, 11, 0, 2, 0, "IMAGE"],
            [18, 34, 0, 2, 1, "STRING"],
        ],
    }


def test_face_swap_reference_traces_resize_to_load_image_and_downstream_prompt():
    graph = face_swap_resize_graph()

    reference = resolve_reference_image_producer(graph)
    assert reference.target is not None
    assert reference.target.producer_node_id == 10
    assert reference.target.direct_producer_node_id == 13
    assert reference.target.route_node_ids == (10, 13, 11)

    prompt = resolve_reference_prompt_producer(graph, reference.target)
    assert prompt.target is not None
    assert prompt.target.producer_node_id == 34
    assert prompt.target.consumer_node_id == 2

    mask = resolve_mask_target(graph, selected_node_ids=[10])
    assert mask.target is not None
    assert mask.target.node_id == 1
    assert mask.reason == "unique_topology_mask_source"


def test_reference_trace_rejects_multi_image_upstream_and_cycles():
    graph = face_swap_resize_graph()
    resize = next(node for node in graph["nodes"] if node["id"] == 13)
    resize["inputs"].append({"name": "second_image", "type": "IMAGE", "link": 99})
    graph["links"].append([99, 1, 0, 13, 1, "IMAGE"])
    assert resolve_reference_image_producer(graph).reason == "no_reference_image"

    graph = face_swap_resize_graph()
    load = next(node for node in graph["nodes"] if node["id"] == 10)
    load["type"] = "ImageResizeKJv2"
    load["inputs"] = [{"name": "image", "type": "IMAGE", "link": 99}]
    graph["links"].append([99, 13, 0, 10, 0, "IMAGE"])
    assert resolve_reference_image_producer(graph).reason == "no_reference_image"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda graph: graph["links"].append([90, 10, -1, 13, 0, "IMAGE"]),
        lambda graph: graph["links"].append([90, 10, 99, 13, 0, "IMAGE"]),
        lambda graph: graph["links"].append([90, 10, 0, 13, -1, "IMAGE"]),
        lambda graph: graph["links"].append([90, 10, 0, 13, 99, "IMAGE"]),
        lambda graph: graph["links"].append([90, 1, 0, 13, 0, "IMAGE"]),
    ],
)
def test_reference_trace_rejects_invalid_or_conflicting_slot_authority(mutate):
    graph = face_swap_resize_graph()
    mutate(graph)

    assert resolve_reference_image_producer(graph).reason == "no_reference_image"


def test_link_type_cannot_manufacture_mask_authority_against_endpoint_types():
    graph = face_swap_resize_graph()
    reference_link = next(link for link in graph["links"] if link[0] == 37)
    reference_link[5] = "MASK"

    assert resolve_reference_image_producer(graph).reason == "no_reference_image"
    resolution = resolve_mask_target(graph, selected_node_ids=[10])
    assert resolution.target is None
    assert resolution.reason == "invalid_workflow_links"


def test_reference_trace_rejects_exact_duplicate_node_ids_but_keeps_typed_ids_distinct():
    graph = face_swap_resize_graph()
    graph["nodes"].append(
        {"id": 10, "type": "LoadImage", "inputs": [], "outputs": []}
    )
    assert resolve_reference_image_producer(graph).reason == "invalid_duplicate_node_ids"

    graph = face_swap_resize_graph()
    graph["nodes"].append(
        {"id": "10", "type": "LoadImage", "inputs": [], "outputs": []}
    )
    resolution = resolve_reference_image_producer(graph)
    assert resolution.target is not None
    assert resolution.target.producer_node_id == 10


def test_context_authority_rejects_invalid_optional_route_ids():
    store = MaskContextTokenStore()
    with pytest.raises(ValueError, match="reference node ID"):
        store.issue(
            session_id="session-a",
            workflow_identity="workflow-a",
            graph_hash="a" * 64,
            node_id=1,
            source_image={"filename": "source.png"},
            reference_node_id=True,
        )

def test_mask_context_token_is_session_bound_expiring_and_one_use():
    now = [100.0]
    store = MaskContextTokenStore(ttl_seconds=5, clock=lambda: now[0])
    token, authority = store.issue(
        session_id="session-a",
        workflow_identity="workflow-a",
        graph_hash="a" * 64,
        node_id=1,
        source_image={"filename": "source.png", "subfolder": "inputs", "type": "input"},
    )

    assert store.inspect(token, session_id="session-a") == authority
    with pytest.raises(ValueError, match="different Ren session"):
        store.inspect(token, session_id="session-b")

    store.consume(token, expected=authority)
    with pytest.raises(ValueError, match="already used"):
        store.inspect(token, session_id="session-a")

    expired, _ = store.issue(
        session_id="session-a",
        workflow_identity="workflow-a",
        graph_hash="a" * 64,
        node_id=1,
        source_image={"filename": "source.png"},
    )
    now[0] = 106.0
    with pytest.raises(ValueError, match="expired"):
        store.inspect(expired, session_id="session-a")


def test_mask_context_token_binds_exact_source_byte_attestation():
    store = MaskContextTokenStore()
    token, authority = store.issue(
        session_id="session-a",
        workflow_identity="workflow-a",
        graph_hash="a" * 64,
        node_id=1,
        source_image={"filename": "source.png"},
        source_attestation={
            "sha256": "b" * 64,
            "size_bytes": 1234,
            "width": 4096,
            "height": 2160,
        },
    )

    assert store.inspect(token, session_id="session-a") == authority
    assert authority.source_attestation == ("b" * 64, 1234, 4096, 2160)
    with pytest.raises(ValueError, match="positive integers"):
        store.issue(
            session_id="session-a",
            workflow_identity="workflow-a",
            graph_hash="a" * 64,
            node_id=1,
            source_image={"filename": "source.png"},
            source_attestation={
                "sha256": "b" * 64,
                "size_bytes": 0,
                "width": 4096,
                "height": 2160,
            },
        )
