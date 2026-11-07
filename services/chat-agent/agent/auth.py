"""Caller identity for every /acp route: the verified Cognito ID token, checked on every request.

CloudFront and the ALB only check that a request came through CloudFront (the secret header); neither checks
the user. So this is the only authentication on the chat path, unlike /a3, where API Gateway's authorizer runs
first. Same checks as the API (services/api/app/auth.py): a key in the pool's JWKS (cached 6 h, refetched only
for an unknown kid), this pool's issuer, the app client as `aud`, `token_use = id`, not expired. The userId is
the `sub`. The raw token is kept too: the agent forwards it to the MCP server, which verifies it again.
"""

import dataclasses
import functools
import os
import re
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "ap-southeast-2_EXAMPLE1")
APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "exampleclientid1234567890")
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"

ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "token_use"]
JWKS_LIFESPAN_SECONDS = 6 * 3600
ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


@dataclasses.dataclass(frozen=True)
class Caller:
    user_id: str
    token: str  # forwarded to the MCP server as the bearer; never logged


@functools.cache
def jwks_client():
    return jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json", lifespan=JWKS_LIFESPAN_SECONDS, timeout=5)


def unauthorized(detail):
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def current_caller(authorization: Annotated[str | None, Header()] = None):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("Missing bearer token")
    try:
        signing_key = jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALGORITHMS,
            audience=APP_CLIENT_ID,
            issuer=ISSUER,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Token keys unavailable")
    except jwt.PyJWTError:
        raise unauthorized("Invalid token")
    if claims["token_use"] != "id":
        raise unauthorized("Invalid token")
    # sub goes into every owner-scoped key (USER#<sub>, quota:<sub>:...), so it must not carry a key delimiter.
    if not isinstance(claims["sub"], str) or not re.fullmatch(ID_PATTERN, claims["sub"]):
        raise unauthorized("Invalid token")
    return Caller(claims["sub"], token)


def caller(request: Request, verified: Annotated[Caller, Depends(current_caller)]):
    """The verified caller, also left on the request for its log line (main.py)."""
    request.state.user_id = verified.user_id
    return verified


CallerDep = Annotated[Caller, Depends(caller)]
