"""Uploads: validate what the client declares, pre-create the Recording, Job, ContextFile and
UsesAsContext items, and hand back one pre-signed POST policy per file.

The browser then posts each file straight to S3. S3 enforces each policy (exact key, exact Content-Type,
size 1..declared), so a client that lies about a size here is still stopped by S3. Capping each policy at
the file's own declared size also makes S3 enforce the 20 MiB context total, which no single policy could.
The S3 notification on recordings/ (ObjectCreated:Post) then starts the job creator.
"""

from typing import Annotated, ClassVar

from contracts import logs
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints, model_validator

from .. import keys, store
from ..auth import UserId
from . import Id

router = APIRouter(prefix="/a3/projects/{project_id}/uploads", tags=["uploads"])

MiB = 1024 * 1024
MAX_RECORDINGS = 5
MAX_RECORDING_BYTES = 200 * MiB
MAX_CONTEXT_FILES = 5
MAX_CONTEXT_TOTAL_BYTES = 20 * MiB
MAX_INSTRUCTION_CHARS = 4000
UPLOAD_EXPIRES_IN = 900  # seconds; the plan's ceiling

# extension -> (Content-Type the policy requires, the types a browser may declare for it). The policy always
# uses the first, so every object of one extension lands with the same type whatever the browser reported.
RECORDING_TYPES = {
    "mp3": ("audio/mpeg", {"audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg-3"}),
    "m4a": ("audio/mp4", {"audio/mp4", "audio/x-m4a", "audio/m4a", "audio/mp4a-latm"}),
    "wav": ("audio/wav", {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}),
    "webm": ("audio/webm", {"audio/webm", "video/webm"}),
    "mp4": ("video/mp4", {"video/mp4", "audio/mp4"}),
}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CONTEXT_TYPES = {
    "pdf": ("application/pdf", {"application/pdf"}),
    "docx": (DOCX, {DOCX}),
    "md": ("text/markdown", {"text/markdown", "text/x-markdown", "text/plain"}),
    "txt": ("text/plain", {"text/plain"}),
}
# What browsers send when they don't know a file's type; treated as "not declared".
UNKNOWN_TYPES = {"", "application/octet-stream"}

# Shown back to the user, never used in a key: no control characters or path separators.
FileName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f/\\]+$")
]


class FileIn(BaseModel):
    TYPES: ClassVar[dict]

    fileName: FileName
    contentType: Annotated[str, StringConstraints(max_length=255)] = ""
    sizeBytes: Annotated[int, Field(ge=1)]

    @property
    def ext(self):
        _, dot, ext = self.fileName.rpartition(".")
        return ext.lower() if dot else ""

    @property
    def upload_content_type(self):
        return self.TYPES[self.ext][0]

    @model_validator(mode="after")
    def check_type(self):
        if self.ext not in self.TYPES:
            raise ValueError(f"{self.fileName}: accepted formats are {', '.join(self.TYPES)}")
        declared = self.contentType.split(";")[0].strip().lower()
        if declared not in UNKNOWN_TYPES and declared not in self.TYPES[self.ext][1]:
            raise ValueError(f"{self.fileName}: content type {declared} doesn't match .{self.ext}")
        return self


class RecordingIn(FileIn):
    TYPES = RECORDING_TYPES
    sizeBytes: Annotated[int, Field(ge=1, le=MAX_RECORDING_BYTES)]


class ContextFileIn(FileIn):
    TYPES = CONTEXT_TYPES
    sizeBytes: Annotated[int, Field(ge=1, le=MAX_CONTEXT_TOTAL_BYTES)]


class UploadRequest(BaseModel):
    schemaId: Annotated[str, StringConstraints(pattern=keys.ID_PATTERN)] = "default"
    instructionText: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_INSTRUCTION_CHARS)] = ""
    recordings: Annotated[list[RecordingIn], Field(min_length=1, max_length=MAX_RECORDINGS)]
    contextFiles: Annotated[list[ContextFileIn], Field(max_length=MAX_CONTEXT_FILES)] = []

    @model_validator(mode="after")
    def check_context_total(self):
        if sum(f.sizeBytes for f in self.contextFiles) > MAX_CONTEXT_TOTAL_BYTES:
            raise ValueError(f"context files total more than {MAX_CONTEXT_TOTAL_BYTES // MiB} MiB")
        return self


@router.post("", status_code=201)
def create_uploads(project_id: Id, body: UploadRequest, user_id: UserId):
    """201: one Queued job per recording, and a POST policy per file. Nothing is processed until a
    recording arrives in S3."""
    store.get_project(user_id, project_id)  # someone else's project is a 404 before anything else
    if not store.schema_exists(user_id, body.schemaId):
        raise HTTPException(422, f"Unknown schemaId {body.schemaId!r}")
    recordings, context_files = store.create_uploads(
        user_id,
        project_id,
        body.schemaId,
        body.instructionText,
        [(f.fileName, f.ext) for f in body.recordings],
        [(f.fileName, f.ext) for f in body.contextFiles],
    )

    for r in recordings:  # where a job's trace starts: the request line has no jobId
        logs.info("job created", userId=user_id, projectId=project_id, jobId=r["jobId"],
                  recordingId=r["recordingId"], stage="Queued")

    def policy(created, declared):
        return store.presign_upload(
            created["storageKey"], declared.upload_content_type, declared.sizeBytes, UPLOAD_EXPIRES_IN
        )

    return {
        "expiresIn": UPLOAD_EXPIRES_IN,
        "recordings": [
            {"recordingId": r["recordingId"], "jobId": r["jobId"], "fileName": r["fileName"], "upload": policy(r, f)}
            for r, f in zip(recordings, body.recordings)
        ],
        "contextFiles": [
            {"contextFileId": c["contextFileId"], "fileName": c["fileName"], "upload": policy(c, f)}
            for c, f in zip(context_files, body.contextFiles)
        ],
    }
