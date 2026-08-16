import hashlib
import io
import json
from pathlib import Path

import pytest
from mask_compositor import (
    MAX_MASK_COMPOSE_BODY_BYTES,
    MaskComposeError,
    compose_attested_mask,
    handle_mask_compose_request,
)
from PIL import Image


def png_bytes(image: Image.Image) -> bytes:
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def source_fixture() -> tuple[bytes, Image.Image]:
    image = Image.new("RGBA", (4, 3))
    pixels = []
    for y in range(image.height):
        for x in range(image.width):
            # Non-black hidden RGB catches browser-canvas premultiplication.
            pixels.append((30 + x * 31, 45 + y * 37, 90 + x * 7 + y, 0 if x < 2 else 255))
    image.putdata(pixels)
    return png_bytes(image), image


def compose(tmp_path: Path, source_bytes: bytes, alpha_bytes: bytes, **overrides):
    expected = {
        "expected_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "expected_size_bytes": len(source_bytes),
        "expected_width": 4,
        "expected_height": 3,
    }
    expected.update(overrides)
    return compose_attested_mask(
        source_bytes=source_bytes,
        alpha_bytes=alpha_bytes,
        input_root=tmp_path,
        **expected,
    )


class FakeMultipartPart:
    def __init__(self, name: str, payload: bytes) -> None:
        self.name = name
        self._payload = payload
        self._offset = 0

    async def read_chunk(self, *, size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeMultipartReader:
    def __init__(self, parts: list[FakeMultipartPart]) -> None:
        self._parts = iter(parts)

    async def next(self) -> FakeMultipartPart | None:
        return next(self._parts, None)


class FakeMultipartRequest:
    def __init__(
        self,
        parts: list[FakeMultipartPart] | None = None,
        *,
        content_type: str = "multipart/form-data",
        content_length: int | None = None,
        multipart_error: Exception | None = None,
    ) -> None:
        self.content_type = content_type
        self.content_length = content_length
        self._parts = parts or []
        self._multipart_error = multipart_error

    async def multipart(self) -> FakeMultipartReader:
        if self._multipart_error is not None:
            raise self._multipart_error
        return FakeMultipartReader(self._parts)


def compose_form(
    source_bytes: bytes,
    alpha_bytes: bytes,
    **overrides,
) -> list[FakeMultipartPart]:
    expected = {
        "expected_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "expected_size_bytes": len(source_bytes),
        "expected_width": 4,
        "expected_height": 3,
    }
    expected.update(overrides)
    parts = [
        FakeMultipartPart("source", source_bytes),
        FakeMultipartPart("alpha", alpha_bytes),
    ]
    for name, value in expected.items():
        parts.append(FakeMultipartPart(name, str(value).encode("ascii")))
    return parts


def test_exact_compositor_preserves_hidden_rgb_and_replaces_only_alpha(tmp_path):
    source_bytes, source = source_fixture()
    desired_alpha = Image.new("L", source.size)
    desired_alpha.putdata([255, 255, 64, 0, 128, 255, 200, 100, 0, 255, 1, 254])
    alpha_transport = Image.new("RGBA", source.size, (255, 0, 170, 255))
    alpha_transport.putalpha(desired_alpha)

    result = compose(tmp_path, source_bytes, png_bytes(alpha_transport))
    output_path = tmp_path / result["subfolder"] / result["name"]

    with Image.open(output_path) as output:
        output = output.convert("RGBA")
        assert output.size == source.size
        for channel in ("R", "G", "B"):
            assert output.getchannel(channel).tobytes() == source.getchannel(channel).tobytes()
        assert output.getchannel("A").tobytes() == desired_alpha.tobytes()
    assert result == {
        "name": output_path.name,
        "subfolder": "fl_mcp_masks",
        "type": "input",
        "width": 4,
        "height": 3,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
    }


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"expected_sha256": "0" * 64}, "mask_source_attestation_mismatch"),
        ({"expected_size_bytes": 1}, "mask_source_attestation_mismatch"),
        ({"expected_width": 5}, "mask_source_attestation_mismatch"),
        ({"expected_height": 4}, "mask_source_attestation_mismatch"),
    ],
)
def test_exact_compositor_rejects_source_attestation_mismatches(
    tmp_path,
    overrides,
    expected_code,
):
    source_bytes, source = source_fixture()
    alpha_bytes = png_bytes(Image.new("RGBA", source.size, (255, 255, 255, 255)))

    with pytest.raises(MaskComposeError) as caught:
        compose(tmp_path, source_bytes, alpha_bytes, **overrides)

    assert caught.value.code == expected_code
    assert caught.value.status == 412
    assert not list(tmp_path.rglob("fl-mcp-mask-*.png"))


def test_exact_compositor_rejects_alpha_dimension_mismatch(tmp_path):
    source_bytes, _source = source_fixture()
    wrong_alpha = png_bytes(Image.new("RGBA", (3, 3), (255, 255, 255, 255)))

    with pytest.raises(MaskComposeError) as caught:
        compose(tmp_path, source_bytes, wrong_alpha)

    assert caught.value.code == "mask_alpha_dimension_mismatch"
    assert caught.value.status == 400
    assert not list(tmp_path.rglob("fl-mcp-mask-*.png"))


@pytest.mark.asyncio
async def test_mask_compose_endpoint_preserves_source_provenance_and_hidden_rgb(tmp_path):
    source_bytes, source = source_fixture()
    desired_alpha = Image.new("L", source.size, 255)
    desired_alpha.putpixel((0, 0), 0)
    alpha = Image.new("RGBA", source.size, (7, 8, 9, 255))
    alpha.putalpha(desired_alpha)
    response = await handle_mask_compose_request(
        FakeMultipartRequest(compose_form(source_bytes, png_bytes(alpha))),
        input_root=tmp_path,
    )
    body = json.loads(response.text)

    assert response.status == 200
    assert body["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert body["width"] == 4
    assert body["height"] == 3
    assert body["subfolder"] == "fl_mcp_masks"
    assert body["name"].startswith("fl-mcp-mask-")
    with Image.open(tmp_path / body["subfolder"] / body["name"]) as output:
        output = output.convert("RGBA")
        for channel in ("R", "G", "B"):
            assert output.getchannel(channel).tobytes() == source.getchannel(channel).tobytes()
        assert output.getchannel("A").tobytes() == desired_alpha.tobytes()


@pytest.mark.asyncio
async def test_mask_compose_endpoint_rejects_mismatch_and_non_multipart(tmp_path):
    source_bytes, source = source_fixture()
    alpha_bytes = png_bytes(Image.new("RGBA", source.size, (255, 255, 255, 255)))
    mismatch = await handle_mask_compose_request(
        FakeMultipartRequest(
            compose_form(source_bytes, alpha_bytes, expected_sha256="0" * 64)
        ),
        input_root=tmp_path,
    )
    mismatch_body = json.loads(mismatch.text)
    wrong_type = await handle_mask_compose_request(
        FakeMultipartRequest(content_type="application/octet-stream"),
        input_root=tmp_path,
    )
    wrong_type_body = json.loads(wrong_type.text)

    assert mismatch.status == 412
    assert mismatch_body["code"] == "mask_source_attestation_mismatch"
    assert wrong_type.status == 415
    assert wrong_type_body["code"] == "invalid_mask_compose_content_type"
    assert not list(tmp_path.rglob("fl-mcp-mask-*.png"))


@pytest.mark.asyncio
async def test_mask_compose_endpoint_rejects_untrusted_fields_and_malformed_bodies(tmp_path):
    source_bytes, source = source_fixture()
    alpha_bytes = png_bytes(Image.new("RGBA", source.size, (255, 255, 255, 255)))

    unknown_parts = compose_form(source_bytes, alpha_bytes)
    unknown_parts.append(FakeMultipartPart("subfolder", b"../escape"))
    unknown = await handle_mask_compose_request(
        FakeMultipartRequest(unknown_parts),
        input_root=tmp_path,
    )
    malformed = await handle_mask_compose_request(
        FakeMultipartRequest(multipart_error=ValueError("malformed boundary")),
        input_root=tmp_path,
    )
    oversized = await handle_mask_compose_request(
        FakeMultipartRequest(content_length=MAX_MASK_COMPOSE_BODY_BYTES + 1),
        input_root=tmp_path,
    )

    assert unknown.status == 400
    assert json.loads(unknown.text)["code"] == "invalid_mask_compose_form"
    assert malformed.status == 400
    assert json.loads(malformed.text)["code"] == "invalid_mask_compose_form"
    assert oversized.status == 413
    assert json.loads(oversized.text)["code"] == "mask_compose_upload_too_large"
    assert not list(tmp_path.rglob("fl-mcp-mask-*.png"))


def test_comfy_route_registers_bounded_exact_mask_compose_endpoint():
    extension = (Path(__file__).parents[1] / "__init__.py").read_text(encoding="utf-8")
    compositor = (Path(__file__).parents[1] / "backend" / "mask_compositor.py").read_text(
        encoding="utf-8"
    )

    assert '@PromptServer.instance.routes.post("/fl_mcp/mask/compose")' in extension
    assert "handle_mask_compose_request" in extension
    assert "_read_bounded_multipart_part" in compositor
    assert '"source": MAX_MASK_SOURCE_BYTES' in compositor
    assert '"alpha": MAX_MASK_ALPHA_BYTES' in compositor
    assert "compose_attested_mask" in compositor
