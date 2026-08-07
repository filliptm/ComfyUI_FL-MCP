"""Shared contracts for images uploaded through the embedded Ren chat."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ChatImageReference(BaseModel):
    """A browser-uploaded image inside Ren's ComfyUI input subfolder."""

    filename: str = Field(..., min_length=1, max_length=255)
    subfolder: str = Field(..., min_length=1, max_length=512)
    type: Literal["input"] = "input"

    @model_validator(mode="after")
    def validate_ren_upload_path(self) -> ChatImageReference:
        filename_path = Path(self.filename)
        subfolder = self.subfolder.replace("\\", "/")
        subfolder_path = Path(subfolder)
        if filename_path.name != self.filename or self.filename in {".", ".."}:
            raise ValueError("Chat image filename must be a basename.")
        if (
            subfolder_path.is_absolute()
            or ".." in subfolder_path.parts
            or not (subfolder == "ren-chat" or subfolder.startswith("ren-chat/"))
        ):
            raise ValueError("Chat images must be inside the ren-chat input folder.")
        self.subfolder = subfolder
        return self

    def widget_value(self) -> str:
        """Return ComfyUI's canonical nested LoadImage widget value."""

        return str(PurePosixPath(self.subfolder) / self.filename)
