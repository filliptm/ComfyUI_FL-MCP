"""Exact-byte mask composition for the ComfyUI browser bridge.

The browser canvas cannot safely round-trip RGB values underneath transparent
pixels because its backing store uses premultiplied alpha.  This module keeps
the attested source bytes opaque to the browser and replaces only their alpha
channel with Pillow.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import re
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any

from aiohttp import web
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_MASK_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MASK_ALPHA_BYTES = 32 * 1024 * 1024
MAX_MASK_DECODED_PIXELS = 64_000_000
MASK_COMPOSE_SUBFOLDER = "fl_mcp_masks"
MAX_MASK_COMPOSE_BODY_BYTES = MAX_MASK_SOURCE_BYTES + MAX_MASK_ALPHA_BYTES + 64 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class MaskComposeError(ValueError):
    """A bounded, user-safe mask composition failure."""

    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _decode_rgba(payload: bytes, *, label: str) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as opened:
                opened.seek(0)
                image = ImageOps.exif_transpose(opened).convert("RGBA")
                image.load()
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise MaskComposeError(
            f"The {label} is not a safe readable image.",
            code=f"invalid_{label.replace(' ', '_')}",
        ) from exc
    if image.width * image.height > MAX_MASK_DECODED_PIXELS:
        raise MaskComposeError(
            f"The {label} exceeds the decoded pixel limit.",
            code=f"{label.replace(' ', '_')}_too_large",
            status=413,
        )
    return image


def compose_attested_mask(
    *,
    source_bytes: bytes,
    alpha_bytes: bytes,
    expected_sha256: str,
    expected_size_bytes: int,
    expected_width: int,
    expected_height: int,
    input_root: Path,
) -> dict[str, Any]:
    """Copy an exact source bitmap's RGB and replace only its alpha channel."""

    if not source_bytes or len(source_bytes) > MAX_MASK_SOURCE_BYTES:
        raise MaskComposeError(
            "The exact mask source exceeds the upload limit.",
            code="mask_source_too_large",
            status=413,
        )
    if not alpha_bytes or len(alpha_bytes) > MAX_MASK_ALPHA_BYTES:
        raise MaskComposeError(
            "The mask alpha exceeds the upload limit.",
            code="mask_alpha_too_large",
            status=413,
        )
    if (
        isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or expected_size_bytes <= 0
        or expected_size_bytes > MAX_MASK_SOURCE_BYTES
    ):
        raise MaskComposeError(
            "The expected source byte size is invalid.",
            code="invalid_source_attestation",
        )
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise MaskComposeError(
            "The expected source SHA-256 is invalid.",
            code="invalid_source_attestation",
        )
    for name, value in (("width", expected_width), ("height", expected_height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MaskComposeError(
                f"The expected source {name} is invalid.",
                code="invalid_source_attestation",
            )
    if expected_width * expected_height > MAX_MASK_DECODED_PIXELS:
        raise MaskComposeError(
            "The expected source dimensions exceed the decoded pixel limit.",
            code="mask_source_too_large",
            status=413,
        )
    if len(source_bytes) != expected_size_bytes:
        raise MaskComposeError(
            "The exact mask source byte size does not match its attestation.",
            code="mask_source_attestation_mismatch",
            status=412,
        )
    observed_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if not hmac.compare_digest(observed_sha256, expected_sha256):
        raise MaskComposeError(
            "The exact mask source bytes do not match their attestation.",
            code="mask_source_attestation_mismatch",
            status=412,
        )

    source = _decode_rgba(source_bytes, label="mask source")
    if source.size != (expected_width, expected_height):
        raise MaskComposeError(
            "The exact mask source dimensions do not match their attestation.",
            code="mask_source_attestation_mismatch",
            status=412,
        )
    alpha = _decode_rgba(alpha_bytes, label="mask alpha")
    if alpha.size != source.size:
        raise MaskComposeError(
            "The mask alpha dimensions do not match the exact source.",
            code="mask_alpha_dimension_mismatch",
        )

    # Pillow preserves RGB samples underneath transparent pixels; only the
    # submitted alpha channel is allowed to change execution pixels.
    source.putalpha(alpha.getchannel("A"))
    output_root = Path(input_root).resolve()
    output_dir = output_root / MASK_COMPOSE_SUBFOLDER
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"fl-mcp-mask-{uuid.uuid4().hex}.png"
    output_path = output_dir / filename
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_dir,
            prefix=".fl-mcp-mask-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        source.save(temporary_path, format="PNG", compress_level=4)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "name": filename,
        "subfolder": MASK_COMPOSE_SUBFOLDER,
        "type": "input",
        "width": source.width,
        "height": source.height,
        "source_sha256": observed_sha256,
    }


async def _read_bounded_multipart_part(part, max_bytes: int) -> bytes:
    payload = bytearray()
    while True:
        chunk = await part.read_chunk(size=64 * 1024)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise MaskComposeError(
                "The mask compose upload exceeds its bounded field limit.",
                code="mask_compose_upload_too_large",
                status=413,
            )
    return bytes(payload)


def _error_response(error: MaskComposeError) -> web.Response:
    return web.json_response(
        {"error": str(error), "code": error.code},
        status=error.status,
    )


async def handle_mask_compose_request(
    request,
    *,
    input_root: Path | None,
) -> web.Response:
    """Validate one bounded multipart request and compose its exact mask."""

    if request.content_type != "multipart/form-data":
        return _error_response(
            MaskComposeError(
                "Mask composition requires multipart form data.",
                code="invalid_mask_compose_content_type",
                status=415,
            )
        )
    if request.content_length is not None and request.content_length > MAX_MASK_COMPOSE_BODY_BYTES:
        return _error_response(
            MaskComposeError(
                "The mask compose upload exceeds the request limit.",
                code="mask_compose_upload_too_large",
                status=413,
            )
        )
    limits = {
        "source": MAX_MASK_SOURCE_BYTES,
        "alpha": MAX_MASK_ALPHA_BYTES,
        "expected_sha256": 128,
        "expected_size_bytes": 128,
        "expected_width": 128,
        "expected_height": 128,
    }
    fields = {}
    try:
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            name = part.name
            if name not in limits or name in fields:
                raise MaskComposeError(
                    "The mask compose form contains an unknown or duplicate field.",
                    code="invalid_mask_compose_form",
                )
            fields[name] = await _read_bounded_multipart_part(part, limits[name])
        missing = sorted(set(limits) - set(fields))
        if missing:
            raise MaskComposeError(
                f"The mask compose form is missing: {', '.join(missing)}.",
                code="invalid_mask_compose_form",
            )
        if input_root is None:
            raise MaskComposeError(
                "The ComfyUI input directory is unavailable.",
                code="mask_compose_storage_unavailable",
                status=503,
            )
        result = await asyncio.to_thread(
            compose_attested_mask,
            source_bytes=fields["source"],
            alpha_bytes=fields["alpha"],
            expected_sha256=fields["expected_sha256"].decode("ascii"),
            expected_size_bytes=int(fields["expected_size_bytes"].decode("ascii")),
            expected_width=int(fields["expected_width"].decode("ascii")),
            expected_height=int(fields["expected_height"].decode("ascii")),
            input_root=input_root,
        )
    except MaskComposeError as error:
        return _error_response(error)
    except (AssertionError, UnicodeDecodeError, ValueError, web.HTTPException):
        return _error_response(
            MaskComposeError(
                "The mask compose multipart form or attestation fields are invalid.",
                code="invalid_mask_compose_form",
            )
        )
    except OSError:
        return _error_response(
            MaskComposeError(
                "The composed mask could not be stored.",
                code="mask_compose_storage_failed",
                status=500,
            )
        )
    return web.json_response(result)


__all__ = [
    "MAX_MASK_ALPHA_BYTES",
    "MAX_MASK_COMPOSE_BODY_BYTES",
    "MAX_MASK_DECODED_PIXELS",
    "MAX_MASK_SOURCE_BYTES",
    "MASK_COMPOSE_SUBFOLDER",
    "MaskComposeError",
    "compose_attested_mask",
    "handle_mask_compose_request",
]
