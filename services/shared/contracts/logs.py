"""Structured log lines and metrics: the fields every service logs, which the saved trace query and
the dashboard depend on. Field and metric names are listed in services/shared/README.md (Log contract).

One JSON object per line on stdout. A Lambda passes a printed line through as it is (its logging handler would
prefix "[INFO]\t<time>\t<requestId>\t"), and the awslogs driver ships each line as one event. So every line is
queryable by its fields in Logs Insights, and a line with an "_aws" block is also an Embedded Metric Format
record, from which CloudWatch makes custom metrics without a PutMetricData call.

Every line has service, level and msg. Lines about a job also carry jobId, userId (the Cognito sub: an opaque
ID, never a name or email) and stage (the job's stage while the line was written); a line that closes a unit
of work carries durationMs. bind() and scope() attach fields to every line that follows, for a process that
works on one message at a time (the Lambdas, the worker). The API serves requests on a thread pool, so it
passes its fields explicitly instead.
"""

import contextlib
import json
import sys
import time

NAMESPACE = "interlixir"

service = "unknown"
_bound = {}
# Where lines go. Tests swap in a list's append to read the records back (capture()).
sink = None


def configure(name):
    global service
    service = name


def bind(**fields):
    """Add fields to every following line (None values are skipped), until the enclosing scope() ends."""
    _bound.update({k: v for k, v in fields.items() if v is not None})


@contextlib.contextmanager
def scope(**fields):
    """Fields for one message or invocation: whatever is bound inside is dropped again on exit."""
    saved = dict(_bound)
    bind(**fields)
    try:
        yield
    finally:
        _bound.clear()
        _bound.update(saved)


def info(msg, **fields):
    _write("INFO", msg, fields)


def warning(msg, **fields):
    _write("WARNING", msg, fields)


def error(msg, **fields):
    """An unexpected failure: also counts one Errors (by Service and in total) for the dashboard."""
    metric("ERROR", msg, {"Errors": (1, "Count")}, {"Service": service}, **fields)


def metric(level, msg, values, dimensions=None, bound=True, **fields):
    """A log line that is also an EMF record. values: {name: (number, unit)}; dimensions: {name: value}. The
    metrics are published per dimension value and in total (one dimension set with them, one without), so a
    total needs no SEARCH or math on the dashboard. bound=False leaves out bound fields (a background thread's
    line that isn't about the current job)."""
    dimensions = dimensions or {}
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [list(dimensions), []] if dimensions else [[]],
                "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in values.items()],
            }],
        },
        **dimensions,
        **{name: value for name, (value, _) in values.items()},
    }
    _write(level, msg, {**record, **fields}, bound)


def _write(level, msg, fields, bound=True):
    record = {"service": service, "level": level, "msg": msg, **(_bound if bound else {}), **fields}
    if sink is not None:
        sink(record)
        return
    print(json.dumps(record, separators=(",", ":"), default=str), file=sys.stdout, flush=True)


@contextlib.contextmanager
def capture():
    """For tests: the records written inside the block, as dicts, instead of printing them."""
    global sink
    saved, records = sink, []
    sink = records.append
    try:
        yield records
    finally:
        sink = saved


def ms_since(started):
    """Whole milliseconds since a time.monotonic() reading."""
    return round((time.monotonic() - started) * 1000)
