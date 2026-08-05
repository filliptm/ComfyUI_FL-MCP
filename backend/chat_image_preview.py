"""Small, bounded previews for ComfyUI images shown in chat."""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

DEFAULT_MAX_DECODED_PIXELS = 64_000_000


class ChatImagePreviewError(RuntimeError):
    """Raised when a ComfyUI image cannot become a safe chat thumbnail."""


@dataclass(frozen=True, slots=True)
class ChatImagePreview:
    content: bytes
    media_type: str
    original_size: tuple[int, int]
    preview_size: tuple[int, int]


def resolve_chat_image_path(root: str | Path, filename: str, subfolder: str = "") -> Path:
    """Resolve an image beneath a known ComfyUI directory without allowing escapes."""

    root_path = Path(root).resolve()
    clean_filename = str(filename or "").strip()
    clean_subfolder = str(subfolder or "").strip()
    if not clean_filename or Path(clean_filename).name != clean_filename:
        raise ChatImagePreviewError("Invalid image filename.")
    if Path(clean_subfolder).is_absolute():
        raise ChatImagePreviewError("Invalid image subfolder.")

    candidate = (root_path / clean_subfolder / clean_filename).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise ChatImagePreviewError("Image path is outside the ComfyUI directory.") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def render_chat_image_preview(
    path: str | Path,
    *,
    max_dimension: int = 256,
    max_decoded_pixels: int = DEFAULT_MAX_DECODED_PIXELS,
) -> ChatImagePreview:
    """Decode one local raster and return a small aspect-preserving thumbnail."""

    if not 64 <= max_dimension <= 512:
        raise ValueError("Chat thumbnail dimensions must be between 64 and 512 pixels.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(path) as source:
                original_size = source.size
                if original_size[0] * original_size[1] > max_decoded_pixels:
                    raise ChatImagePreviewError(
                        f"Image exceeds the {max_decoded_pixels:,}-pixel preview limit."
                    )
                source.seek(0)
                image = ImageOps.exif_transpose(source).copy()
    except ChatImagePreviewError:
        raise
    except (OSError, UnidentifiedImageError, PILImage.DecompressionBombError) as exc:
        raise ChatImagePreviewError("File is not a readable image.") from exc
    except PILImage.DecompressionBombWarning as exc:
        raise ChatImagePreviewError("Image dimensions are too large to preview.") from exc

    image.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)
    preview_size = image.size
    has_alpha = "A" in image.getbands() or (
        image.mode == "P" and "transparency" in image.info
    )
    buffer = io.BytesIO()
    if has_alpha:
        image.save(buffer, format="PNG", optimize=True)
        media_type = "image/png"
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
        media_type = "image/jpeg"
    return ChatImagePreview(
        content=buffer.getvalue(),
        media_type=media_type,
        original_size=original_size,
        preview_size=preview_size,
    )
