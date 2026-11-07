"""One router per resource. All of them sit under /a3 and take the caller from auth.UserId."""

from typing import Annotated

from fastapi import Depends, Header, Path

from .. import keys

# Path parameter for any ID. A value outside the pattern (e.g. containing '#') gets 422 before any lookup.
Id = Annotated[str, Path(pattern=keys.ID_PATTERN)]


def _no_cache(cache_control: Annotated[str, Header()] = "") -> bool:
    """The client's `Cache-Control: no-cache`: skip the cached copy (M12; the load test's baseline run)."""
    return "no-cache" in cache_control.lower()


NoCache = Annotated[bool, Depends(_no_cache)]
