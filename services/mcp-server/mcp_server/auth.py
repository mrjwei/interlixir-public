"""The caller of a tool: the `sub` of the Cognito ID token the agent forwards on every MCP request.

The agent has verified the same token already. It's checked again here because the tools take the userId from
nowhere else: not from the model's arguments, not from a header the agent could set to any value. Same checks as
the API (services/api/app/auth.py): signed by a key in the pool's JWKS (cached 6 h, refetched only for an unknown
kid), this pool's issuer, the app client as `aud`, `token_use = id`, not expired, and a `sub` that is safe in a key.
"""

import functools
import os
import re

import jwt

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "ap-southeast-2_EXAMPLE1")
APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "exampleclientid1234567890")
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"

ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "token_use"]
JWKS_LIFESPAN_SECONDS = 6 * 3600
ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


class Unauthorized(Exception):
    """No token, or one that fails a check. The message is safe to show the model."""


@functools.cache
def jwks_client():
    return jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json", lifespan=JWKS_LIFESPAN_SECONDS, timeout=5)


def user_id(authorization):
    """The verified userId from an `Authorization: Bearer <id token>` value."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token")
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
        raise Unauthorized("token keys unavailable") from None
    except jwt.PyJWTError:
        raise Unauthorized("invalid token") from None
    if claims["token_use"] != "id":
        raise Unauthorized("invalid token")
    if not isinstance(claims["sub"], str) or not re.fullmatch(ID_PATTERN, claims["sub"]):
        raise Unauthorized("invalid token")
    return claims["sub"]
