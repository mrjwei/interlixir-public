"""The model price table: read from Parameter Store, cached, and never fatal."""

import json
import unittest
from unittest import mock

from contracts import logs, prices

TABLE = {"m": {"input": 1.0, "output": 2.0}}


class FakeSsm:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.names = value, error, []

    def get_parameter(self, Name):
        self.names.append(Name)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


class PricesTest(unittest.TestCase):
    def setUp(self):
        prices.reset()
        self.now = 1000.0
        self.enterContext(mock.patch.dict("os.environ", {"MODEL_PRICES_PARAMETER": "/p", "MODEL_PRICES": ""}))
        self.enterContext(mock.patch.object(prices.time, "monotonic", lambda: self.now))
        self.records = self.enterContext(logs.capture())

    def use(self, ssm):
        self.enterContext(mock.patch.object(prices, "_ssm", lambda: ssm))
        return ssm

    def test_reads_the_named_parameter_and_caches_it(self):
        ssm = self.use(FakeSsm(json.dumps(TABLE)))
        self.assertEqual(prices.get(), TABLE)
        self.now += prices.TTL_SECONDS - 1
        prices.get()
        self.assertEqual(ssm.names, ["/p"])

    def test_a_changed_parameter_shows_after_the_ttl_without_a_redeploy(self):
        ssm = self.use(FakeSsm(json.dumps(TABLE)))
        prices.get()
        ssm.value = json.dumps({"m": {"input": 5.0, "output": 6.0}})
        self.assertEqual(prices.get()["m"]["input"], 1.0)
        self.now += prices.TTL_SECONDS
        self.assertEqual(prices.get()["m"]["input"], 5.0)

    def test_a_failed_read_keeps_the_last_good_table(self):
        ssm = self.use(FakeSsm(json.dumps(TABLE)))
        prices.get()
        ssm.error = TimeoutError("slow")
        self.now += prices.TTL_SECONDS
        self.assertEqual(prices.get(), TABLE)
        self.assertEqual(len(ssm.names), 2)

    def test_without_a_good_read_the_environment_is_the_fallback(self):
        self.use(FakeSsm(error=RuntimeError("denied")))
        with mock.patch.dict("os.environ", {"MODEL_PRICES": json.dumps(TABLE)}):
            self.assertEqual(prices.get(), TABLE)

    def test_an_invalid_table_is_not_used(self):
        ssm = self.use(FakeSsm("[1, 2]"))
        self.assertEqual(prices.get(), {})
        self.assertEqual(len(ssm.names), 1)

    def test_an_empty_parameter_name_turns_ssm_off(self):
        ssm = self.use(FakeSsm(json.dumps(TABLE)))
        with mock.patch.dict("os.environ", {"MODEL_PRICES_PARAMETER": "", "MODEL_PRICES": json.dumps(TABLE)}):
            self.assertEqual(prices.get(), TABLE)
        self.assertEqual(ssm.names, [])


if __name__ == "__main__":
    unittest.main()
