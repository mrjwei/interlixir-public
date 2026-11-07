"""Projects: the sidebar list, create, rename, delete, and one project's contents (both reads cached for 10 s:
M12). Uploads into a project are in uploads.py."""

from typing import Annotated

from fastapi import APIRouter, Response
from pydantic import BaseModel, StringConstraints

from .. import cache, keys, store
from ..auth import UserId
from . import Id, NoCache

router = APIRouter(prefix="/a3/projects", tags=["projects"])


class NewProject(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def _project(item):
    return {
        "projectId": keys.strip(item["SK"], keys.PROJECT_PREFIX),
        "name": item["projectName"],
        "createdAt": item.get("createdAt"),
    }


@router.get("")
def list_projects(user_id: UserId, response: Response, no_cache: NoCache):
    return cache.aside(
        response, "projects", cache.projects_key(user_id), cache.LIST_TTL,
        lambda: {"projects": [_project(item) for item in store.list_projects(user_id)]}, no_cache,
    )


@router.post("", status_code=201)
def create_project(body: NewProject, user_id: UserId):
    return _project(store.create_project(user_id, body.name))


@router.patch("/{project_id}")
def rename_project(project_id: Id, body: NewProject, user_id: UserId):
    return _project(store.rename_project(user_id, project_id, body.name))


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: Id, user_id: UserId):
    """204 once the Project item is gone; GC removes what was in it asynchronously (DeleteRequested)."""
    store.delete_project(user_id, project_id)
    return Response(status_code=204)


@router.get("/{project_id}")
def get_project(project_id: Id, user_id: UserId, response: Response, no_cache: NoCache):
    return cache.aside(
        response, "project", cache.project_key(user_id, project_id), cache.LIST_TTL,
        lambda: _project_contents(user_id, project_id), no_cache,
    )


def _project_contents(user_id, project_id):
    project = _project(store.get_project(user_id, project_id))
    recordings, documents = [], []
    for item in store.list_project_contents(user_id, project_id):
        sk = item["SK"]
        if sk.startswith(keys.REC_PREFIX):
            recordings.append(
                {"recordingId": keys.strip(sk, keys.REC_PREFIX), "fileName": item.get("fileName"), "jobId": item.get("jobId")}
            )
        elif sk.startswith(keys.DOC_PREFIX):
            documents.append(
                {"documentId": keys.strip(sk, keys.DOC_PREFIX), "title": item.get("documentTitle"), "jobId": item.get("jobId")}
            )
    return {**project, "recordings": recordings, "documents": documents}
