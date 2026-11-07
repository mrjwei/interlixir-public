"""Documents: read one, export it as a Markdown download, rename or edit it, and delete it.

Nested under the project because a Document's key is PROJECT#<projectId> / DOC#<documentId>: there is no
lookup by documentId alone without a new index.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Response
from pydantic import BaseModel, StringConstraints, model_validator

from .. import keys, store
from ..auth import UserId
from . import Id

router = APIRouter(prefix="/a3/projects/{project_id}/documents", tags=["documents"])

EXPORT_EXPIRES_IN = 300  # seconds
MAX_MARKDOWN_CHARS = 500_000  # a generated document is 5-20k; this leaves room for long notes, far below S3's limits


class DocumentChange(BaseModel):
    """Either or both. The title is what the sidebar, the page and the export's file name show."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    markdown: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MARKDOWN_CHARS)] | None = None

    @model_validator(mode="after")
    def _something(self):
        if self.title is None and self.markdown is None:
            raise ValueError("Send a title, a markdown body, or both")
        return self


def _filename(title):
    """A Content-Disposition-safe file name: ASCII letters, digits, space, '_', '-', '.'."""
    name = re.sub(r"[^A-Za-z0-9 _.-]+", "", title or "").strip(" .")[:80]
    return f"{name or 'document'}.md"


@router.get("/{document_id}")
def get_document(project_id: Id, document_id: Id, user_id: UserId):
    item = store.get_document(user_id, project_id, document_id)
    return {
        "documentId": document_id,
        "projectId": project_id,
        "jobId": item.get("jobId"),
        "title": item.get("documentTitle"),
        "markdown": store.read_document(user_id, project_id, document_id),
        # What the document was made from (M9's document view); the instruction and schema are on the job.
        "contextFiles": [
            {"contextFileId": keys.strip(c["SK"], keys.CTX_PREFIX), "fileName": c.get("fileName")}
            for c in store.job_context_files(project_id, item.get("jobId"))
        ],
    }


@router.get("/{document_id}/export")
def export_document(project_id: Id, document_id: Id, user_id: UserId):
    item = store.get_document(user_id, project_id, document_id)
    filename = _filename(item.get("documentTitle"))
    url = store.presign_document_download(user_id, project_id, document_id, filename, EXPORT_EXPIRES_IN)
    return {"url": url, "fileName": filename, "expiresIn": EXPORT_EXPIRES_IN}


@router.patch("/{document_id}")
def update_document(project_id: Id, document_id: Id, body: DocumentChange, user_id: UserId):
    item = store.update_document(user_id, project_id, document_id, title=body.title, markdown=body.markdown)
    return {"documentId": document_id, "projectId": project_id, "title": item.get("documentTitle"),
            "updatedAt": item.get("updatedAt")}


@router.delete("/{document_id}", status_code=204)
def delete_document(project_id: Id, document_id: Id, user_id: UserId):
    """204 once the Document and its Recording are gone; GC removes the rest asynchronously (DeleteRequested)."""
    store.delete_document(user_id, project_id, document_id)
    return Response(status_code=204)
