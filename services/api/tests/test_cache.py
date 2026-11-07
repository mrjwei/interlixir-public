"""Cache-aside, invalidation and quota counters against an in-memory fake with Memcached's
semantics (relative TTLs, `add` only if absent, `incr` keeps the TTL, 250-byte keys). pymemcache's own
MockMemcacheClient drops the TTL on incr, so it can't test the quota window.

TestRealMemcached runs the same calls against a real server when MEMCACHED_TEST_ENDPOINT is set, e.g.
  docker run --rm -d -p 11211:11211 memcached:1.6 && MEMCACHED_TEST_ENDPOINT=localhost:11211 make test-api
"""

import contextlib
import datetime
import io
import json
import os
import socket
import ssl
import unittest
from unittest import mock

from pymemcache.exceptions import MemcacheUnexpectedCloseError

from app import cache, keys, store

from .test_api import ALICE, BOB, DOC, JOB, REC, ApiTestCase

UTC = datetime.timezone.utc


class FakeMemcache:
    def __init__(self):
        self.now = 1_000_000.0
        self.data = {}  # key -> (value bytes, expires at or None)
        self.calls = []

    def _check(self, key):
        assert isinstance(key, str) and len(key.encode()) <= 250 and not any(c.isspace() for c in key), key

    def _live(self, key):
        value, expires = self.data.get(key, (None, None))
        if value is not None and expires is not None and expires <= self.now:
            del self.data[key]
            return None
        return value

    def get(self, key):
        self._check(key)
        self.calls.append(("get", key))
        return self._live(key)

    def set(self, key, value, expire=0, noreply=None):
        self._check(key)
        self.calls.append(("set", key))
        self.data[key] = (value if isinstance(value, bytes) else str(value).encode(), self.now + expire if expire else None)
        return True

    def add(self, key, value, expire=0, noreply=None):
        if self._live(key) is not None:
            return False
        return self.set(key, value, expire)

    def incr(self, key, value, noreply=False):
        current = self._live(key)
        if current is None:
            return None
        total = int(current) + value
        self.data[key] = (str(total).encode(), self.data[key][1])
        return total

    def delete_many(self, keys_, noreply=None):
        self.calls.append(("delete", *keys_))
        for key in keys_:
            self.data.pop(key, None)
        return True

    def ttl_of(self, key):
        return self.data[key][1] - self.now


class Broken:
    """A node that's gone: every call fails the way pymemcache does."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls += 1
            raise ConnectionRefusedError(111, "Connection refused")

        return fail


class CacheTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.mc = FakeMemcache()
        self.use(self.mc)
        cache._down_until = 0.0
        self.addCleanup(setattr, cache, "_down_until", 0.0)
        self.project_id = self.create_project()

    def use(self, client):
        patcher = mock.patch.object(cache, "client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def poll(self, path, user=ALICE, **headers):
        response = self.client.get(path, headers={"X-Test-User": user, **headers})
        return response, response.headers.get(cache.HEADER)


class JobStatus(CacheTestCase):
    def setUp(self):
        super().setUp()
        self.put_job(self.project_id, stage="Transcribing", chunksTotal=3, doneChunks={0})

    def set_stage(self, stage):
        store.table().update_item(
            Key={"PK": keys.job_pk(JOB), "SK": keys.JOB_SK},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "stage"},
            ExpressionAttributeValues={":s": stage},
        )

    def test_repeated_polls_hit_until_the_ttl(self):
        first, state = self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual((first.status_code, state), (200, "MISS"))
        self.set_stage("Synthesising")  # the pipeline's write: it can't invalidate
        second, state = self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual(state, "HIT")
        self.assertEqual(second.json(), first.json())  # served from the cache, not DynamoDB
        self.assertEqual(self.mc.ttl_of(cache.job_key(ALICE, JOB)), cache.JOB_TTL)

        self.mc.now += cache.JOB_TTL
        third, state = self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual((state, third.json()["stage"]), ("MISS", "Synthesising"))

    def test_retry_invalidates_so_the_next_poll_is_a_miss(self):
        self.set_stage("Failed")
        self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual(self.poll(f"/a3/jobs/{JOB}")[1], "HIT")
        self.assertEqual(self.post(f"/a3/jobs/{JOB}/retry").status_code, 202)
        self.assertIn(("delete", cache.job_key(ALICE, JOB)), self.mc.calls)
        response, state = self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual((state, response.json()["stage"], response.json()["retryCount"]), ("MISS", "Queued", 1))

    def test_rejected_retry_leaves_the_entry(self):
        self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual(self.post(f"/a3/jobs/{JOB}/retry").status_code, 409)
        self.assertEqual(self.poll(f"/a3/jobs/{JOB}")[1], "HIT")

    def test_no_cache_header_bypasses_and_refreshes(self):
        self.poll(f"/a3/jobs/{JOB}")
        self.set_stage("Synthesising")
        response, state = self.poll(f"/a3/jobs/{JOB}", **{"Cache-Control": "no-cache"})
        self.assertEqual((state, response.json()["stage"]), ("BYPASS", "Synthesising"))
        response, state = self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual((state, response.json()["stage"]), ("HIT", "Synthesising"))

    def test_another_users_poll_never_sees_the_cached_job(self):
        self.poll(f"/a3/jobs/{JOB}")
        response, state = self.poll(f"/a3/jobs/{JOB}", BOB)
        self.assertEqual((response.status_code, state), (404, None))
        self.assertNotIn(cache.job_key(BOB, JOB), self.mc.data)

    def test_not_found_is_not_cached(self):
        other = "01JA3M8Q2V7K9XG4T5N6P1R2S4"
        self.assertEqual(self.poll(f"/a3/jobs/{other}")[0].status_code, 404)
        self.assertEqual([k for k in self.mc.data if other in k], [])

    def test_unreadable_value_is_a_miss_and_is_replaced(self):
        self.mc.set(cache.job_key(ALICE, JOB), b"\x80not json", expire=60)
        response, state = self.poll(f"/a3/jobs/{JOB}")
        self.assertEqual((response.status_code, state), (200, "MISS"))
        self.assertEqual(json.loads(self.mc.get(cache.job_key(ALICE, JOB)))["stage"], "Transcribing")


class ProjectLists(CacheTestCase):
    def test_create_invalidates_the_list(self):
        self.poll("/a3/projects")
        self.assertEqual(self.poll("/a3/projects")[1], "HIT")
        new_id = self.create_project(name="Second study")
        response, state = self.poll("/a3/projects")
        self.assertEqual(state, "MISS")
        self.assertIn(new_id, [p["projectId"] for p in response.json()["projects"]])

    def test_lists_are_per_user(self):
        self.poll("/a3/projects")
        response, state = self.poll("/a3/projects", BOB)
        self.assertEqual((state, response.json()), ("MISS", {"projects": []}))

    def test_upload_invalidates_the_projects_contents(self):
        store.table().put_item(Item={"PK": keys.BUILTIN_SCHEMA_PK, "SK": "SCHEMA#default", "name": "Default"})
        path = f"/a3/projects/{self.project_id}"
        self.poll(path)
        self.assertEqual(self.poll(path)[1], "HIT")
        upload = self.post(f"{path}/uploads", json={"recordings": [{"fileName": "p1.mp3", "sizeBytes": 1000}]})
        self.assertEqual(upload.status_code, 201, upload.text)
        response, state = self.poll(path)
        self.assertEqual((state, [r["fileName"] for r in response.json()["recordings"]]), ("MISS", ["p1.mp3"]))
        self.assertEqual(self.mc.ttl_of(cache.project_key(ALICE, self.project_id)), cache.LIST_TTL)

    def test_rename_and_delete_invalidate_both_lists(self):
        # the project list shows the name, the project's own read shows it too.
        path = f"/a3/projects/{self.project_id}"
        for _ in range(2):
            self.poll("/a3/projects")
            self.poll(path)
        self.assertEqual(self.client.patch(path, headers={"X-Test-User": ALICE}, json={"name": "Renamed"}).status_code, 200)
        listed, state = self.poll("/a3/projects")
        self.assertEqual((state, listed.json()["projects"][0]["name"]), ("MISS", "Renamed"))
        project, state = self.poll(path)
        self.assertEqual((state, project.json()["name"]), ("MISS", "Renamed"))
        self.poll("/a3/projects")
        self.assertEqual(self.client.delete(path, headers={"X-Test-User": ALICE}).status_code, 204)
        listed, state = self.poll("/a3/projects")
        self.assertEqual((state, listed.json()["projects"]), ("MISS", []))
        self.assertEqual(self.poll(path)[0].status_code, 404)

    def test_document_rename_and_delete_invalidate_the_projects_contents(self):
        path = f"/a3/projects/{self.project_id}"
        store.table().put_item(Item={"PK": keys.project_pk(self.project_id), "SK": keys.rec_sk(REC), "jobId": JOB})
        self.put_document(self.project_id)
        self.poll(path)
        self.assertEqual(self.poll(path)[1], "HIT")
        self.client.patch(f"{path}/documents/{DOC}", headers={"X-Test-User": ALICE}, json={"title": "Renamed"})
        response, state = self.poll(path)
        self.assertEqual((state, response.json()["documents"][0]["title"]), ("MISS", "Renamed"))
        self.client.delete(f"{path}/documents/{DOC}", headers={"X-Test-User": ALICE})
        response, state = self.poll(path)
        self.assertEqual((state, response.json()["documents"], response.json()["recordings"]), ("MISS", [], []))

    def test_someone_elses_project_is_404_and_not_cached(self):
        self.poll(f"/a3/projects/{self.project_id}")
        response, _ = self.poll(f"/a3/projects/{self.project_id}", BOB)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(cache.project_key(BOB, self.project_id), self.mc.data)


class HistoryPages(CacheTestCase):
    def setUp(self):
        super().setUp()
        for n in range(7):
            store.table().put_item(
                Item={"PK": keys.user_pk(ALICE), "SK": f"EVENT#2026-10-15T01:02:0{n}.000Z#evt{n}", "eventType": "JobFailed"}
            )

    def test_each_page_is_cached_by_cursor(self):
        first, state = self.poll("/a3/history")
        self.assertEqual(state, "MISS")
        cursor = first.json()["nextCursor"]
        self.assertEqual(self.poll("/a3/history")[1], "HIT")
        self.assertEqual(self.poll(f"/a3/history?cursor={cursor}")[1], "MISS")
        self.assertEqual(self.poll(f"/a3/history?cursor={cursor}")[1], "HIT")
        self.assertEqual(self.poll("/a3/history?limit=10")[1], "MISS")
        self.assertEqual(self.mc.ttl_of(cache.history_key(ALICE, 5, None)), cache.HISTORY_TTL)

    def test_a_bad_cursor_is_rejected_before_the_cache(self):
        response, state = self.poll("/a3/history?cursor=not-a-cursor")
        self.assertEqual((response.status_code, state), (400, None))


class Unavailable(CacheTestCase):
    def test_no_endpoint_reads_dynamodb(self):
        self.use(None)
        self.assertEqual(self.poll("/a3/projects")[1], "OFF")
        self.assertEqual(self.poll("/a3/projects")[1], "OFF")

    def test_down_node_fails_open_then_backs_off(self):
        broken = Broken()
        self.use(broken)
        response, state = self.poll("/a3/projects")
        self.assertEqual((response.status_code, state), (200, "OFF"))
        self.assertEqual(broken.calls, 1)
        self.create_project(name="Written anyway")  # the write succeeds; its invalidation is skipped
        self.assertEqual(self.poll("/a3/projects")[1], "OFF")
        self.assertEqual(broken.calls, 1)  # skipped for BACKOFF_SECONDS

        cache._down_until = 0.0  # the backoff has passed
        self.poll("/a3/projects")
        self.assertEqual(broken.calls, 2)

    def test_closed_connection_on_set_still_returns_the_value(self):
        self.mc.set = mock.Mock(side_effect=MemcacheUnexpectedCloseError())
        response, state = self.poll("/a3/projects")
        self.assertEqual((response.status_code, state), (200, "MISS"))
        self.assertGreater(cache._down_until, 0)


class Client(unittest.TestCase):
    def client_with(self, tls):
        with mock.patch.object(cache, "CACHE_ENDPOINT", "node.example.cache.amazonaws.com:11211"), \
                mock.patch.object(cache, "CACHE_TLS", tls):
            cache.client.cache_clear()
            self.addCleanup(cache.client.cache_clear)
            return cache.client()

    def test_tls_verifies_the_nodes_certificate(self):
        c = self.client_with(True)
        self.assertEqual(c.server, ("node.example.cache.amazonaws.com", 11211))
        self.assertEqual(c.tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(c.tls_context.check_hostname)

    def test_plain_without_tls(self):
        self.assertIsNone(self.client_with(False).tls_context)


class Metrics(CacheTestCase):
    def test_one_emf_line_per_cached_read(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.poll("/a3/projects")
            self.poll("/a3/projects")
        miss, hit = [json.loads(line) for line in out.getvalue().splitlines() if '"msg":"cache"' in line]
        directive = hit["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(directive["Namespace"], "interlixir")
        self.assertEqual(directive["Dimensions"], [["Route"], []])
        self.assertEqual([m["Name"] for m in directive["Metrics"]], ["CacheHit", "CacheMiss"])
        self.assertIsInstance(hit["_aws"]["Timestamp"], int)
        self.assertEqual((miss["cache"], miss["CacheHit"], miss["CacheMiss"]), ("miss", 0, 1))
        self.assertEqual((hit["cache"], hit["Route"], hit["CacheHit"], hit["CacheMiss"]), ("hit", "projects", 1, 0))


class Keys(CacheTestCase):
    def test_long_ids_stay_within_memcacheds_limit(self):
        long_id = "x" * 128
        key = cache.job_key(long_id, long_id)
        self.assertLessEqual(len(key), cache.MAX_KEY_BYTES)
        self.assertTrue(key.startswith("job:sha256:"))
        self.assertNotEqual(key, cache.job_key(long_id, "y" * 128))
        self.assertEqual(cache.job_key(ALICE, JOB), f"job:{ALICE}:{JOB}")

    def test_quota_key_is_the_hour_window(self):
        start, _ = cache.window(datetime.datetime(2026, 10, 18, 9, 41, 7, tzinfo=UTC))
        self.assertEqual(cache.quota_key(ALICE, start), f"quota:{ALICE}:2026101809")


class Quota(CacheTestCase):
    now = datetime.datetime(2026, 10, 18, 9, 59, 30, tzinfo=UTC)

    def test_counts_within_the_window_and_resets_at_the_hour(self):
        reset = datetime.datetime(2026, 10, 18, 10, tzinfo=UTC)
        self.assertEqual(cache.count_in_window(ALICE, 1200, self.now), (1200, reset))
        self.assertEqual(cache.count_in_window(ALICE, 800, self.now), (2000, reset))
        self.assertEqual(cache.count_in_window(BOB, 5, self.now), (5, reset))
        key = cache.quota_key(ALICE, cache.window(self.now)[0])
        self.assertEqual(self.mc.ttl_of(key), 30 + 60)  # the window's rest plus slack; incr kept it

        later = self.now + datetime.timedelta(seconds=31)
        self.assertEqual(cache.count_in_window(ALICE, 7, later), (7, reset + datetime.timedelta(hours=1)))

    def test_evicted_between_add_and_incr(self):
        real_add = self.mc.add
        evict_once = iter([True])

        def add_then_evict(key, *args, **kwargs):
            result = real_add(key, *args, **kwargs)
            if next(evict_once, False):
                self.mc.data.pop(key)
            return result

        self.mc.add = add_then_evict
        self.assertEqual(cache.count_in_window(ALICE, 3, self.now)[0], 3)

    def test_no_cache_is_none_so_the_caller_decides(self):
        self.use(None)
        self.assertIsNone(cache.count_in_window(ALICE, 1, self.now))
        self.use(Broken())
        self.assertIsNone(cache.count_in_window(ALICE, 1, self.now))


class TestRealMemcached(ApiTestCase):
    """The pymemcache calls against a real server (skipped unless MEMCACHED_TEST_ENDPOINT is set)."""

    def setUp(self):
        endpoint = os.environ.get("MEMCACHED_TEST_ENDPOINT")
        if not endpoint:
            self.skipTest("MEMCACHED_TEST_ENDPOINT not set")
        super().setUp()
        patcher = mock.patch.object(cache, "CACHE_ENDPOINT", endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.client.cache_clear()
        self.addCleanup(cache.client.cache_clear)
        cache._down_until = 0.0
        self.mc = cache.client()
        self.addCleanup(self.mc.close)
        self.mc.flush_all()

    def test_aside_invalidate_and_quota(self):
        project_id = self.create_project()
        self.put_job(project_id, stage="Failed")
        states = [self.get(f"/a3/jobs/{JOB}").headers[cache.HEADER] for _ in range(2)]
        self.assertEqual(states, ["MISS", "HIT"])
        self.post(f"/a3/jobs/{JOB}/retry")
        response = self.get(f"/a3/jobs/{JOB}")
        self.assertEqual((response.headers[cache.HEADER], response.json()["stage"]), ("MISS", "Queued"))

        now = datetime.datetime.now(UTC)
        self.assertEqual(cache.count_in_window(ALICE, 10, now)[0], 10)
        self.assertEqual(cache.count_in_window(ALICE, 5, now)[0], 15)

    def test_unreachable_node_fails_open(self):
        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with mock.patch.object(cache, "CACHE_ENDPOINT", f"127.0.0.1:{port}"):
            cache.client.cache_clear()
            self.assertEqual(self.get("/a3/projects").headers[cache.HEADER], "OFF")
        cache.client.cache_clear()
