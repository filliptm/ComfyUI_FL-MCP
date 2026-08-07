from types import SimpleNamespace

import mcp_server
import pytest
from node_library import CompatibleNode, NodeCatalogSnapshot, NodeSearchResult


def fake_context():
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"client": None})
    )


def search_result(node_type, score):
    return NodeSearchResult(
        node_type=node_type,
        display_name=node_type,
        category="test",
        description="",
        inputs={},
        outputs=["IMAGE"],
        match_reason="test match",
        origin="custom",
        python_module="custom_nodes.example",
        schema_hash=node_type.lower().ljust(64, "0")[:64],
        score=score,
    )


def compatible(node_type, direction):
    return CompatibleNode(
        node_type=node_type,
        display_name=node_type,
        category="test",
        direction=direction,
        connection={
            "source_output": "IMAGE",
            "target_input": "image",
            "data_type": "IMAGE",
        },
        description="",
    )


class FakeSearchClient:
    def __init__(self):
        self.max_results = None

    async def search_nodes(self, **kwargs):
        self.max_results = kwargs["max_results"]
        return [
            search_result("Exact", 100),
            search_result("Second", 80),
            search_result("Hidden", 70),
        ]

    async def catalog_status(self):
        return {
            "state": "fresh",
            "node_count": 3,
            "catalog_hash": "a" * 64,
        }


@pytest.mark.asyncio
async def test_mcp_search_preserves_provenance_and_exact_truncation(monkeypatch):
    client = FakeSearchClient()
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.node_library_search.fn(
        mcp_server.NodeLibrarySearchRequest(query="example", max_results=2),
        fake_context(),
    )

    assert client.max_results == 3
    assert result["truncated"] is True
    assert [item["node_type"] for item in result["results"]] == ["Exact", "Second"]
    assert result["results"][0]["origin"] == "custom"
    assert result["results"][0]["python_module"] == "custom_nodes.example"
    assert result["catalog"]["catalog_hash"] == "a" * 64


class FakeCompatibilityClient:
    def __init__(self, results):
        self.results = results
        self.max_results = None

    async def find_compatible_nodes(self, **kwargs):
        self.max_results = kwargs["max_results"]
        return self.results

    async def catalog_status(self):
        return {"state": "fresh", "node_count": 10, "catalog_hash": "b" * 64}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "expected_truncated"),
    [
        (
            [
                compatible("DownA", "downstream"),
                compatible("DownB", "downstream"),
                compatible("UpA", "upstream"),
                compatible("UpB", "upstream"),
            ],
            False,
        ),
        (
            [
                compatible("DownA", "downstream"),
                compatible("DownB", "downstream"),
                compatible("DownHidden", "downstream"),
                compatible("UpA", "upstream"),
                compatible("UpB", "upstream"),
            ],
            True,
        ),
    ],
)
async def test_mcp_compatible_truncation_is_per_direction(
    monkeypatch,
    results,
    expected_truncated,
):
    client = FakeCompatibilityClient(results)
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.node_library_find_compatible.fn(
        mcp_server.NodeLibraryFindCompatibleRequest(
            node_type="Source",
            direction="both",
            max_results=2,
        ),
        fake_context(),
    )

    assert client.max_results == 3
    assert result["truncated"] is expected_truncated
    assert [item["direction"] for item in result["compatible_nodes"]] == [
        "downstream",
        "downstream",
        "upstream",
        "upstream",
    ]


class FakePlannerClient:
    source = "http://127.0.0.1:8188/object_info"

    def __init__(self):
        self.catalog = {
            "Source": {
                "input": {"required": {}},
                "output": ["IMAGE"],
                "output_name": ["IMAGE"],
                "python_module": "nodes",
            }
        }

    async def catalog_snapshot(self):
        return NodeCatalogSnapshot(
            data=self.catalog,
            source=self.source,
            catalog_hash="c" * 64,
            observed_catalog_hash="d" * 64,
            catalog_hash_schema="test",
            fetched_at=1.0,
            expires_at=2.0,
        )


@pytest.mark.asyncio
async def test_mcp_plan_workflow_is_a_read_only_catalog_pinned_dry_run(monkeypatch):
    client = FakePlannerClient()
    monkeypatch.setattr(mcp_server, "get_node_library_client", lambda **kwargs: client)

    result = await mcp_server.plan_workflow.fn(
        mcp_server.PlanWorkflowRequest(
            nodes=[{"alias": "source", "node_type": "Source"}],
            expected_catalog_hash="c" * 64,
        ),
        fake_context(),
    )

    assert result["valid"] is True
    assert result["catalog"]["catalog_hash"] == "c" * 64
    assert result["plan"]["nodes"][0]["schema_hash"]
