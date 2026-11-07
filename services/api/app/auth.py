"""Caller identity. Every /a3 route takes its userId from `UserId`, and from nowhere else.

API Gateway's JWT authorizer has already checked the token; this checks it again (defence in depth):
anyone in the shared account who can read the integration can learn the ALB's secret header. The Cognito
ID token must be signed by a key in the pool's JWKS (fetched once, cached, refetched only for an unknown
kid), carry this pool's issuer and the app client as `aud`, have `token_use = id`, and not be expired.
The caller's userId is its `sub`.
"""

import functools
import os
import re
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request

from . import keys

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "ap-southeast-2_EXAMPLE1")
APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "exampleclientid1234567890")
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"

# Cognito signs with RS256 only. Pinning it stops "alg": "none" and HS256-with-the-public-key tricks.
ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "token_use"]
JWKS_LIFESPAN_SECONDS = 6 * 3600


@functools.cache
def jwks_client():
    return jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json", lifespan=JWKS_LIFESPAN_SECONDS, timeout=5)


def unauthorized(detail):
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def current_user(authorization: Annotated[str | None, Header()] = None):
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
        # Can't reach the JWKS (NAT or Cognito trouble): fail closed, but don't blame the caller's token.
        raise HTTPException(503, "Token keys unavailable")
    except jwt.PyJWTError:
        raise unauthorized("Invalid token")
    if claims["token_use"] != "id":
        raise unauthorized("Invalid token")
    # sub becomes part of every owner-scoped key (USER#<sub>), so it must not carry a key delimiter.
    if not isinstance(claims["sub"], str) or not re.fullmatch(keys.ID_PATTERN, claims["sub"]):
        raise unauthorized("Invalid token")
    return claims["sub"]


def caller(request: Request, user_id: Annotated[str, Depends(current_user)]):
    """The verified userId, also left on the request for its log line (main.py)."""
    request.state.user_id = user_id
    return user_id


UserId = Annotated[str, Depends(caller)]
