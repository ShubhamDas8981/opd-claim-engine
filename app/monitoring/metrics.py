"""
Put this file at app/monitoring/metrics.py

All Prometheus metrics of the OPD Wallet live here, so the rest of the code only calls small helpers
(guard_event, track_llm, claim_decision ...). If `prometheus_client` is not installed every helper does nothing,
so the app never breaks because of monitoring.

Two processes expose metrics:
  * the API   -> GET /metrics on port 8000            (HTTP, guardrails, chat, LLM intent calls, claim decisions, DB gauges)
  * the worker -> start_worker_metrics_server(8001)   (extractions, LLM extraction/audit calls, worker DB errors)
Prometheus scrapes both (see monitoring/prometheus/prometheus.yml).
"""
import re
import time
from contextlib import contextmanager

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest, start_http_server,
    )
    from prometheus_client.core import GaugeMetricFamily
    ENABLED = True
except Exception:                                   # monitoring is optional
    ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain"

NS = "opd"
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
LLM_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60)

if ENABLED:
    # ---------------------------------------------------------------- HTTP (API)
    HTTP_REQUESTS = Counter(f"{NS}_http_requests_total", "HTTP requests", ["method", "route", "status"])
    HTTP_LATENCY = Histogram(f"{NS}_http_request_duration_seconds", "HTTP request latency", ["method", "route"],
                             buckets=LATENCY_BUCKETS)
    HTTP_IN_PROGRESS = Gauge(f"{NS}_http_requests_in_progress", "HTTP requests being served right now")

    # ---------------------------------------------------------------- guardrails
    GUARD_EVENTS = Counter(f"{NS}_guard_events_total", "Guardrail blocks and detections", ["kind", "rule"])
    CHAT_MESSAGES = Counter(f"{NS}_chat_messages_total", "Chat messages by input-guard result", ["result"])
    UPLOADS = Counter(f"{NS}_uploads_total", "Bill uploads by result", ["result"])
    RATE_LIMITED = Counter(f"{NS}_rate_limited_total", "Requests refused by the per-user rate limit")
    PII_REDACTIONS = Counter(f"{NS}_pii_redactions_total", "Sensitive values masked", ["source", "kind"])
    OUTPUT_REJECTED = Counter(f"{NS}_llm_output_rejected_total", "LLM answers rejected by the output guards", ["reason"])
    GUARDRAILS_AI = Gauge(f"{NS}_guardrails_ai_enabled", "1 when the guardrails-ai package is active, 0 when only the built-in rules run")

    # ---------------------------------------------------------------- LLM
    LLM_REQUESTS = Counter(f"{NS}_llm_requests_total", "LLM calls", ["purpose", "outcome"])
    LLM_LATENCY = Histogram(f"{NS}_llm_request_duration_seconds", "LLM call latency", ["purpose"], buckets=LLM_BUCKETS)
    LLM_TOKENS = Counter(f"{NS}_llm_tokens_total", "LLM tokens used", ["purpose", "type"])
    INTENTS = Counter(f"{NS}_chat_intents_total", "Chat intents chosen by the router", ["tool"])

    # ---------------------------------------------------------------- claims pipeline
    EXTRACTIONS = Counter(f"{NS}_extractions_total", "Bill extractions", ["outcome"])
    EXTRACTION_LATENCY = Histogram(f"{NS}_extraction_duration_seconds", "Time to read and audit one bill",
                                   buckets=LLM_BUCKETS)
    EXTRACTION_CONFIDENCE = Histogram(f"{NS}_extraction_confidence_score", "LLM audit score of each bill (0-100)",
                                      buckets=(10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100))
    BILLS_FLAGGED = Counter(f"{NS}_bills_flagged_total", "Bills that got at least one red flag")
    CLAIM_DECISIONS = Counter(f"{NS}_claim_decisions_total", "Claim submit outcomes", ["status"])
    APPROVED_INR = Counter(f"{NS}_approved_amount_inr_total", "Rupees auto-approved and paid out")
    WORKER_EVENTS = Counter(f"{NS}_worker_events_total", "Kafka claim events handled by the worker", ["outcome"])
    DB_ERRORS = Counter(f"{NS}_db_errors_total", "Database errors", ["operation", "kind"])

    BUILD = Gauge(f"{NS}_build_info", "Process info", ["component"])


# ---------------------------------------------------------------- helpers (all safe to call without prometheus_client)
_SAFE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _label(value, default="other"):
    """Keeps label values short and from a small set, so metrics cannot explode in size."""
    value = str(value or "").strip().lower()
    return value if _SAFE.match(value) else default


def init(component, guardrails_ai=False):
    if ENABLED:
        BUILD.labels(component).set(1)
        GUARDRAILS_AI.set(1 if guardrails_ai else 0)


def render():
    """Body for GET /metrics."""
    return generate_latest() if ENABLED else b"# prometheus_client is not installed\n"


def start_worker_metrics_server(port=8001):
    """Call once in the Kafka worker process."""
    if ENABLED:
        start_http_server(port)
        BUILD.labels("worker").set(1)


def in_progress(delta):
    if ENABLED:
        HTTP_IN_PROGRESS.inc(delta)


def http_request(method, route, status, seconds):
    if ENABLED:
        HTTP_REQUESTS.labels(method, route, str(status)).inc()
        HTTP_LATENCY.labels(method, route).observe(seconds)


def guard_event(kind, detail=""):
    """kind: chat-blocked | bill-injection | upload-blocked. detail: comma separated rule names."""
    if not ENABLED:
        return
    if kind == "upload-blocked":
        rules_ = ["upload"]
    else:
        rules_ = [_label(r, "guardrails-ai") for r in str(detail).split(",") if r.strip()] or ["unknown"]
    for r in rules_:
        GUARD_EVENTS.labels(_label(kind), r).inc()


def chat_message(result):                 # allowed | injection | too-long | empty
    if ENABLED:
        CHAT_MESSAGES.labels(_label(result)).inc()


def upload(result):                       # accepted | refused
    if ENABLED:
        UPLOADS.labels(_label(result)).inc()


def rate_limited():
    if ENABLED:
        RATE_LIMITED.inc()


def pii_redacted(source, kinds):
    if ENABLED:
        for k in kinds or []:
            PII_REDACTIONS.labels(_label(source), _label(k)).inc()


def output_rejected(reason):
    if ENABLED:
        OUTPUT_REJECTED.labels(_label(reason)).inc()


def intent(tool):
    if ENABLED:
        INTENTS.labels(_label(tool)).inc()


class _LLMTracker:
    def __init__(self, purpose):
        self.purpose = purpose

    def usage(self, completion):
        """Call with the Groq response to count tokens."""
        try:
            u = completion.usage
            LLM_TOKENS.labels(self.purpose, "prompt").inc(u.prompt_tokens or 0)
            LLM_TOKENS.labels(self.purpose, "completion").inc(u.completion_tokens or 0)
        except Exception:
            pass


def _outcome(exc):
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "ratelimit" in name or "429" in text:
        return "rate_limited"
    if "timeout" in name:
        return "timeout"
    if "json_validate_failed" in text or "badrequest" in name:
        return "bad_json"
    return "error"


@contextmanager
def track_llm(purpose):
    """with track_llm("intent") as t:  completion = client.chat...create(...);  t.usage(completion)"""
    tracker = _LLMTracker(_label(purpose))
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield tracker
    except Exception as exc:
        outcome = _outcome(exc)
        raise
    finally:
        if ENABLED:
            LLM_REQUESTS.labels(tracker.purpose, outcome).inc()
            LLM_LATENCY.labels(tracker.purpose).observe(time.perf_counter() - start)


@contextmanager
def track_extraction():
    start = time.perf_counter()
    try:
        yield
    finally:
        if ENABLED:
            EXTRACTION_LATENCY.observe(time.perf_counter() - start)


def extraction(outcome, score=None, flagged=False):     # ok | failed | empty
    if ENABLED:
        EXTRACTIONS.labels(_label(outcome)).inc()
        if score is not None:
            EXTRACTION_CONFIDENCE.observe(max(0, min(100, float(score))))
        if flagged:
            BILLS_FLAGGED.inc()


def claim_decision(status, amount=0.0):                 # approved | pending | low_balance | already_submitted | error
    if ENABLED:
        CLAIM_DECISIONS.labels(_label(status)).inc()
        if status == "approved" and amount:
            APPROVED_INR.inc(float(amount))


def worker_event(outcome):                              # received | saved | failed
    if ENABLED:
        WORKER_EVENTS.labels(_label(outcome)).inc()


def db_error(operation, exc=None):
    """kind is 'duplicate' for MySQL error 1062, else the exception class name."""
    if ENABLED:
        kind = "duplicate" if exc is not None and "1062" in str(exc) else _label(type(exc).__name__ if exc else "error")
        DB_ERRORS.labels(_label(operation), kind).inc()


# ---------------------------------------------------------------- business gauges read from MySQL at scrape time
class _ClaimsCollector:
    """Claims per status, users, and the age of the oldest claim waiting for a person (the review SLA)."""
    CACHE_SECONDS = 15

    def __init__(self):
        self._at = 0.0
        self._data = None

    def describe(self):
        return []                      # do not query MySQL while registering

    def _load(self):
        if self._data is not None and time.time() - self._at < self.CACHE_SECONDS:
            return self._data
        from sqlalchemy import text
        from app.worker.claims_worker import SessionLocal
        db = SessionLocal()
        try:
            by_status = {s or "UNKNOWN": int(n) for s, n in db.execute(
                text("SELECT status, COUNT(*) FROM claims GROUP BY status")).fetchall()}
            oldest = db.execute(text(
                "SELECT COALESCE(TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()), 0) FROM claims WHERE status = 'PENDING'"
            )).scalar()
            users = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
            self._data = (by_status, float(oldest or 0), int(users or 0))
            self._at = time.time()
        finally:
            db.close()
        return self._data

    def collect(self):
        up = GaugeMetricFamily(f"{NS}_db_up", "1 when the metrics collector could read MySQL")
        try:
            by_status, oldest, users = self._load()
        except Exception:
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        g = GaugeMetricFamily(f"{NS}_claims_by_status", "Claims in the database by status", labels=["status"])
        for status, n in by_status.items():
            g.add_metric([_label(status)], n)
        yield g
        a = GaugeMetricFamily(f"{NS}_oldest_pending_claim_age_seconds", "Age of the oldest claim waiting for human review")
        a.add_metric([], oldest)
        yield a
        u = GaugeMetricFamily(f"{NS}_users", "Registered users")
        u.add_metric([], users)
        yield u


_registered = False


def register_db_collector():
    """Call once in the API process."""
    global _registered
    if ENABLED and not _registered:
        REGISTRY.register(_ClaimsCollector())
        _registered = True
