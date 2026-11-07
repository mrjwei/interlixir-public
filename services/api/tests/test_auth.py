"""Token verification tests (auth.current_user). Run with `make test-api`.

A local RSA key plays Cognito: its public half is served as the pool's JWKS by patching PyJWKClient's fetch,
so the real kid lookup, signature, issuer, audience, expiry and token_use checks all run, offline.
"""

import time
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth, main

SUB = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
KID = "test-key-1"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


POOL_KEY = rsa_key()
OTHER_KEY = rsa_key()


def jwks(*private_keys_by_kid):
    keys = []
    for kid, private_key in private_keys_by_kid:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": keys}


def id_token(key=POOL_KEY, kid=KID, algorithm="RS256", drop=(), **overrides):
    now = int(time.time())
    claims = {
        "sub": SUB,
        "iss": auth.ISSUER,
        "aud": auth.APP_CLIENT_ID,
        "token_use": "id",
        "iat": now,
        "exp": now + 3600,
        "email": "alice@example.com",
        **overrides,
    }
    for name in drop:
        claims.pop(name)
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


class CurrentUser(unittest.TestCase):
    def setUp(self):
        auth.jwks_client.cache_clear()
        self.addCleanup(auth.jwks_client.cache_clear)
        self.fetch = mock.patch.object(jwt.PyJWKClient, "fetch_data", return_value=jwks((KID, POOL_KEY))).start()
        self.addCleanup(mock.patch.stopall)
        # /a3/projects is a real route whose only other dependency is the table; the store call is stubbed so
        # a 200 proves the token was accepted and shows which userId reached the store.
        self.list_projects = mock.patch("app.store.list_projects", return_value=[]).start()
        self.client = TestClient(main.app)

    def call(self, token=None, authorization=None):
        headers = {}
        if authorization is not None:
            headers["Authorization"] = authorization
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.get("/a3/projects", headers=headers)

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.list_projects.assert_not_called()

    def test_valid_id_token_gives_its_sub(self):
        response = self.call(id_token())
        self.assertEqual(response.status_code, 200, response.text)
        self.list_projects.assert_called_once_with(SUB)

    def test_jwks_is_fetched_once(self):
        for _ in range(3):
            self.assertEqual(self.call(id_token()).status_code, 200)
        self.assertEqual(self.fetch.call_count, 1)

    def test_missing_or_malformed_header(self):
        for authorization in (None, "", "Bearer", "Bearer ", "Basic abc", id_token(), "Bearer not.a.jwt"):
            with self.subTest(authorization=authorization):
                self.assert_rejected(self.call(authorization=authorization))

    def test_expired(self):
        self.assert_rejected(self.call(id_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 60)))

    def test_wrong_audience(self):
        self.assert_rejected(self.call(id_token(aud="some-other-client")))

    def test_wrong_issuer(self):
        self.assert_rejected(self.call(id_token(iss="https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_other")))

    def test_access_token_is_not_accepted(self):
        # Cognito access tokens carry client_id, not aud, and token_use = access.
        self.assert_rejected(self.call(id_token(token_use="access", drop=("aud",), client_id=auth.APP_CLIENT_ID)))
        self.assert_rejected(self.call(id_token(token_use="access")))

    def test_required_claims(self):
        for claim in ("sub", "token_use", "exp"):
            with self.subTest(claim=claim):
                self.assert_rejected(self.call(id_token(drop=(claim,))))

    def test_signed_by_another_key(self):
        self.assert_rejected(self.call(id_token(key=OTHER_KEY)))
        self.assert_rejected(self.call(id_token(key=OTHER_KEY, kid="unknown-kid")))

    def test_algorithm_is_pinned(self):
        self.assert_rejected(self.call(id_token(key=None, algorithm="none")))
        self.assert_rejected(self.call(id_token(key="a-shared-secret-of-at-least-32-bytes!!", algorithm="HS256")))

    def test_sub_with_key_delimiter(self):
        for sub in ("a#PROJECT#b", "a/b", "", "x" * 129):
            with self.subTest(sub=sub):
                self.assert_rejected(self.call(id_token(sub=sub)))

    def test_jwks_unreachable_is_503(self):
        self.fetch.side_effect = jwt.PyJWKClientConnectionError("timed out")
        response = self.call(id_token())
        self.assertEqual(response.status_code, 503)
        self.list_projects.assert_not_called()

    def test_healthz_needs_no_token(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
