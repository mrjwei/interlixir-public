"""The log contract: one JSON object per line, bound fields, and EMF records CloudWatch can extract."""

import contextlib
import io
import json
import unittest

from contracts import logs


class Lines(unittest.TestCase):
    def setUp(self):
        saved = logs.service
        self.addCleanup(logs.configure, saved)
        logs.configure("test-service")

    def test_one_compact_json_object_per_line_on_stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            logs.info("hello", jobId="j1", n=1)
            logs.warning("careful")
        first, second = (json.loads(line) for line in out.getvalue().splitlines())
        self.assertEqual(first, {"service": "test-service", "level": "INFO", "msg": "hello", "jobId": "j1", "n": 1})
        self.assertEqual(second["level"], "WARNING")
        self.assertNotIn(" ", out.getvalue().splitlines()[0])  # separators without spaces

    def test_scope_binds_fields_until_it_ends(self):
        with logs.capture() as records:
            with logs.scope(messageId="m1"):
                logs.bind(jobId="j1", userId=None)  # None is not bound
                logs.info("inside")
            logs.info("after")
        inside, after = records
        self.assertEqual((inside["messageId"], inside["jobId"]), ("m1", "j1"))
        self.assertNotIn("userId", inside)
        self.assertNotIn("jobId", after)

    def test_explicit_fields_win_over_bound_ones(self):
        with logs.capture() as records, logs.scope(stage="Queued"):
            logs.info("moved", stage="Transcribing")
        self.assertEqual(records[0]["stage"], "Transcribing")


class Metrics(unittest.TestCase):
    def test_emf_record_with_a_dimension_and_a_total(self):
        with logs.capture() as records:
            logs.metric("INFO", "step", {"StepDuration": (1200, "Milliseconds")}, {"Step": "merge"}, jobId="j1")
        [record] = records
        [directive] = record["_aws"]["CloudWatchMetrics"]
        self.assertEqual(directive["Namespace"], "interlixir")
        self.assertEqual(directive["Dimensions"], [["Step"], []])
        self.assertEqual(directive["Metrics"], [{"Name": "StepDuration", "Unit": "Milliseconds"}])
        self.assertIsInstance(record["_aws"]["Timestamp"], int)
        self.assertEqual((record["Step"], record["StepDuration"], record["jobId"]), ("merge", 1200, "j1"))

    def test_no_dimensions_publishes_the_total_only(self):
        with logs.capture() as records:
            logs.metric("INFO", "done", {"JobsDone": (1, "Count")})
        self.assertEqual(records[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [[]])

    def test_error_counts_errors_by_service(self):
        with logs.capture() as records:
            logs.error("boom", error="RuntimeError()")
        [record] = records
        self.assertEqual((record["level"], record["Errors"], record["Service"]), ("ERROR", 1, logs.service))

    def test_unbound_metric_leaves_out_the_current_job(self):
        with logs.capture() as records, logs.scope(jobId="j1"):
            logs.metric("INFO", "task time", {"WorkerTaskSeconds": (60, "Seconds")}, bound=False)
        self.assertNotIn("jobId", records[0])


if __name__ == "__main__":
    unittest.main()
