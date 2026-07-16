from typing import Literal

from pydantic import BaseModel


class PreSignedUrlRequest(BaseModel):
    filename: str
    content_type: str | None = None
    operation: Literal["upload", "download"]
