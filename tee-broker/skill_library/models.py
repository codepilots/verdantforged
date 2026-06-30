import re
from pydantic import BaseModel, Field, field_validator

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9._-]+)?$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class SkillCard(BaseModel):
    name: str
    version: str
    description: str = Field(min_length=1, max_length=2000)
    license: str = "Apache-2.0"
    summary: str = Field(default="", max_length=280)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must match [a-z0-9][a-z0-9_-]{1,63}")
        return v

    @field_validator("version")
    @classmethod
    def _version_ok(cls, v: str) -> str:
        if not _VERSION_RE.match(v):
            raise ValueError("version must be semver (e.g. 1.0.0 or 1.0.0-rc.1)")
        return v


class SkillFileRef(BaseModel):
    filename: str
    sha256: str
    size_bytes: int = Field(ge=0)
    content_type: str = "application/octet-stream"

    @field_validator("sha256")
    @classmethod
    def _sha_ok(cls, v: str) -> str:
        if not _HEX_RE.match(v):
            raise ValueError("sha256 must be 64-char hex")
        return v.lower()


class SkillFileUpload(BaseModel):
    sha256: str | None = None
    content_type: str = "application/octet-stream"

    @field_validator("sha256")
    @classmethod
    def _sha_ok(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _HEX_RE.match(v):
            raise ValueError("sha256 must be 64-char hex")
        return v.lower()


class SkillCardWithFiles(SkillCard):
    files: list[SkillFileRef] = []
    total_bytes: int = 0