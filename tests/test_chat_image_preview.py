import io

import pytest
from chat_image_preview import (
    ChatImagePreviewError,
    render_chat_image_preview,
    resolve_chat_image_path,
)
from PIL import Image


def test_chat_image_preview_is_small_and_keeps_aspect_ratio(tmp_path):
    source = tmp_path / "wide.png"
    Image.new("RGB", (2400, 1200), (50, 100, 180)).save(source)

    preview = render_chat_image_preview(source, max_dimension=256)

    assert preview.original_size == (2400, 1200)
    assert preview.preview_size == (256, 128)
    assert preview.media_type == "image/jpeg"
    with Image.open(io.BytesIO(preview.content)) as rendered:
        assert rendered.size == (256, 128)


def test_chat_image_preview_preserves_transparency(tmp_path):
    source = tmp_path / "alpha.png"
    Image.new("RGBA", (400, 200), (50, 100, 180, 120)).save(source)

    preview = render_chat_image_preview(source)

    assert preview.media_type == "image/png"
    with Image.open(io.BytesIO(preview.content)) as rendered:
        assert rendered.size == (256, 128)
        assert "A" in rendered.getbands()


def test_chat_image_path_stays_inside_comfy_directory(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    image = nested / "result.png"
    Image.new("RGB", (20, 20)).save(image)

    assert resolve_chat_image_path(root, "result.png", "nested") == image.resolve()
    with pytest.raises(ChatImagePreviewError, match="outside"):
        resolve_chat_image_path(root, "outside.png", "../../")
    with pytest.raises(ChatImagePreviewError, match="filename"):
        resolve_chat_image_path(root, "../result.png")
