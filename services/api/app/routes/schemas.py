"""Schemas (templates) for the upload page: the 6 built-ins plus the user's own."""

from fastapi import APIRouter

from .. import keys, store
from ..auth import UserId

router = APIRouter(prefix="/a3/schemas", tags=["schemas"])


def _schema(item, builtin):
    return {
        "schemaId": keys.strip(item["SK"], keys.SCHEMA_PREFIX),
        "name": item.get("name"),
        "description": item.get("description"),
        "builtIn": builtin,
    }


@router.get("")
def list_schemas(user_id: UserId):
    builtin, own = store.list_schemas(user_id)
    return {"schemas": [_schema(i, True) for i in builtin] + [_schema(i, False) for i in own]}
