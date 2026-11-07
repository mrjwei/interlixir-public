"""Test fixtures shared by the MCP server's and the chat agent's tests: fake AWS credentials, a moto table and
docs bucket seeded with two users' data, and real Cognito-style ID tokens.

A local RSA key plays Cognito: its public half is served as the pool's JWKS by patching PyJWKClient's fetch, so
both services' real token checks run offline. The chat agent's tests load this file by path (both test folders
are packages named `tests`).
"""

import os

# Fake credentials before boto3 creates anything, so nothing can reach the real account.
os.environ.pop("AWS_PROFILE", None)
os.environ.update(
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_SESSION_TOKEN="testing",
    AWS_REGION="ap-southeast-2",
    AWS_DEFAULT_REGION="ap-southeast-2",
)

import decimal
import time
from unittest import mock

import boto3
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

REGION = "ap-southeast-2"
TABLE = "interlixir-data"
DOCS_BUCKET = "interlixir-docs"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/ap-southeast-2_EXAMPLE1"
APP_CLIENT_ID = "exampleclientid1234567890"
KID = "test-key-1"
POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

ALICE = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
BOB = "9b2e6f1a-0c3d-4e5f-8a7b-6c5d4e3f2a1b"

# Alice: one project with two finished usability tests and one recording still transcribing.
ALICE_PROJECT = "01a1a000-0000-7000-8000-000000000001"
ALICE_DOC_1 = "01a1a000-0000-7000-8000-0000000000d1"
ALICE_DOC_2 = "01a1a000-0000-7000-8000-0000000000d2"
ALICE_PENDING_JOB = "01a1a000-0000-7000-8000-0000000000j3"
# Bob: one project, one document with a secret only Bob may see.
BOB_PROJECT = "01b0b000-0000-7000-8000-000000000001"
BOB_DOC = "01b0b000-0000-7000-8000-0000000000d1"
BOB_SECRET = "BOB-ONLY-7f3a: the onboarding wizard confused every participant"


def jwks():
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}


def patch_jwks(test):
    """Serve the test key as the pool's JWKS for the rest of the test (every PyJWKClient, any thread)."""
    patcher = mock.patch.object(jwt.PyJWKClient, "fetch_data", return_value=jwks())
    patcher.start()
    test.addCleanup(patcher.stop)


def id_token(sub=ALICE, **overrides):
    now = int(time.time())
    claims = {"sub": sub, "iss": ISSUER, "aud": APP_CLIENT_ID, "token_use": "id", "iat": now, "exp": now + 3600,
              **overrides}
    return jwt.encode(claims, POOL_KEY, algorithm="RS256", headers={"kid": KID})


def create_resources():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "GSI1",
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "KEYS_ONLY"},
        }],
    )
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DOCS_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
    )


def table():
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def put_document(user, project, doc, title, markdown):
    table().put_item(Item={"PK": f"PROJECT#{project}", "SK": f"DOC#{doc}", "documentTitle": title, "userId": user,
                           "jobId": doc, "schemaId": "usability-test", "createdAt": "2026-10-20T01:00:00.000Z"})
    table().put_item(Item={"PK": f"JOB#{doc}", "SK": "META", "GSI1PK": f"PROJECT#{project}", "GSI1SK": f"JOB#{doc}",
                           "userId": user, "stage": "Done", "audioSeconds": 1800, "tokenCount": 12000, "cost": decimal.Decimal("0.04")})
    table().put_item(Item={"PK": f"PROJECT#{project}", "SK": f"REC#{doc}", "fileName": f"{title}.m4a", "jobId": doc})
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=DOCS_BUCKET, Key=f"documents/{user}/{project}/{doc}.md", Body=markdown.encode()
    )


def seed():
    t = table()
    t.put_item(Item={"PK": f"USER#{ALICE}", "SK": f"PROJECT#{ALICE_PROJECT}", "projectName": "Checkout redesign",
                     "createdAt": "2026-10-19T01:00:00.000Z"})
    put_document(ALICE, ALICE_PROJECT, ALICE_DOC_1, "Checkout usability test — Participant 3",
                 "# Checkout usability test — Participant 3\n\n## Findings\n- Delivery fees appear late.\n")
    put_document(ALICE, ALICE_PROJECT, ALICE_DOC_2, "Lab session — Participant 7",
                 "# Lab session — Participant 7\n\n## Findings\n- Delivery costs surprised them at payment.\n")
    t.put_item(Item={"PK": f"PROJECT#{ALICE_PROJECT}", "SK": "REC#pending", "fileName": "p4.m4a",
                     "jobId": ALICE_PENDING_JOB})
    t.put_item(Item={"PK": f"JOB#{ALICE_PENDING_JOB}", "SK": "META", "GSI1PK": f"PROJECT#{ALICE_PROJECT}",
                     "GSI1SK": f"JOB#{ALICE_PENDING_JOB}", "userId": ALICE, "stage": "Transcribing"})
    for n, (event_type, cost) in enumerate([("UploadReceived", None), ("SynthesisDone", decimal.Decimal("0.04"))]):
        item = {"PK": f"USER#{ALICE}", "SK": f"EVENT#2026-10-20T0{n}:00:00.000Z#evt-{n}", "eventType": event_type,
                "jobId": ALICE_DOC_1}
        if cost:
            item.update(tokenCount=12000, cost=cost)
        t.put_item(Item=item)

    t.put_item(Item={"PK": f"USER#{BOB}", "SK": f"PROJECT#{BOB_PROJECT}", "projectName": "Onboarding research",
                     "createdAt": "2026-10-19T02:00:00.000Z"})
    put_document(BOB, BOB_PROJECT, BOB_DOC, "Onboarding interview", f"# Onboarding interview\n\n{BOB_SECRET}\n")
    t.put_item(Item={"PK": f"USER#{BOB}", "SK": "EVENT#2026-10-20T05:00:00.000Z#evt-bob", "eventType": "UploadReceived",
                     "jobId": BOB_DOC})


_server_url = None


def mcp_server_url():
    """The real MCP server app on uvicorn in a background thread (once per test process): http://127.0.0.1:<port>/mcp.

    Started once because the SDK's session manager runs only once per app. moto's mock is process-wide, so the
    server thread's AWS calls hit the same fake table as the test."""
    global _server_url
    if _server_url is None:
        import socket
        import threading

        import uvicorn
        from mcp_server import server

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        instance = uvicorn.Server(config)
        threading.Thread(target=instance.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not instance.started:
            if time.monotonic() > deadline:
                raise RuntimeError("MCP server didn't start")
            time.sleep(0.05)
        _server_url = f"http://127.0.0.1:{port}/mcp"
    return _server_url
