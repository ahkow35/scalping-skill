"""Railway entry point for the flow recorder — runs recorder.py's capture
loop as its own Railway service (alongside railway_watch.py, PR A), uploads
each finished UTC-day compressed file to a private Railway Storage Bucket
(S3-compatible), and serves a small `/health` + `/status` HTTP address the
watcher can optionally poll (PR C, ACCOUNT-MONITOR.md, RECORDER-RAILWAY.md).

Read-only toward the exchange: recorder.py only subscribes to public market
data. This module never places, cancels or closes an order and adds no
exchange read of its own. The only new network surface here is the S3-
compatible bucket API, entirely separate from the exchange.

CLI usage: `python3 railway_record.py`. Environment variables read on boot:

- `RECORDER_COINS` — comma-separated coin list (default `HYPE`).
- `DATA_DIR` — the Railway volume path for the recorder's own files and the
  upload ledger (default `/data`).
- `PORT` — the port the HTTP server listens on (default `8080`), bound to
  `::` (dual-stack) since Railway private networking is IPv6.
- `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` —
  the bucket's S3 API credentials. Missing any one of these disables
  uploads (a clear `/status` field and alert condition) — recording
  continues regardless; this is never a fatal startup error.
- `S3_REGION` — optional, default `auto`.
- `S3_ADDRESSING_STYLE` — optional, `virtual` (default) or `path` — older
  Railway buckets may need `path`.
- `RECORDER_KEEP_DAYS` — days past a confirmed-uploaded day's end before its
  local `.gz` is deleted (default 3).

Never prints or logs the S3 secret access key or access key id, including
inside exceptions and the `/status` body — see UploadError/_error_code.
"""

import asyncio
import glob
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import recorder as rec

logger = logging.getLogger("railway_record")

DEFAULT_COINS = ["HYPE"]
DEFAULT_KEEP_DAYS = 3
DEFAULT_REGION = "auto"
DEFAULT_ADDRESSING_STYLE = "virtual"
STATUS_INTERVAL_S = 30
UPLOAD_RETRY_S = 600  # 10 minutes — a failed upload never stops recording
# A snapshot this stale means the recorder's own status loop has stalled
# (e.g. the whole process is wedged) — read as silent rather than
# frozen-fresh, instead of trusting a snapshot lag_s that stopped advancing.
SNAPSHOT_STALE_MULTIPLE = 3

# HEAD errors that mean "object absent" — anything else (a real network
# failure, 403 from a missing ListBucket grant, etc.) is an inconclusive
# read and must never be treated as license to PUT.
NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


# ── S3 config: parsed from env, never raises — missing config just means
#    uploads are disabled (recording continues); see s3_config_from_env. ────

@dataclass
class S3Config:
    endpoint: str
    region: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    addressing_style: str


def s3_config_from_env(env):
    """None if any required S3 variable is missing — the caller disables
    uploads rather than failing to start (recording must continue without a
    bucket). Never raises."""
    endpoint = env.get("S3_ENDPOINT")
    bucket = env.get("S3_BUCKET")
    access_key_id = env.get("S3_ACCESS_KEY_ID")
    secret_access_key = env.get("S3_SECRET_ACCESS_KEY")
    if not (endpoint and bucket and access_key_id and secret_access_key):
        return None
    return S3Config(
        endpoint=endpoint,
        region=env.get("S3_REGION", DEFAULT_REGION),
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        addressing_style=env.get("S3_ADDRESSING_STYLE", DEFAULT_ADDRESSING_STYLE),
    )


def make_real_s3_client(config):
    """Build the live boto3 S3 client for `config`. Imported lazily so this
    module — and its pure/testable helpers — stay importable without boto3
    installed; only a real deploy with S3 env configured ever calls this."""
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        # boto3 >= 1.36 defaults PUT bodies to aws-chunked transfer with a
        # trailing CRC32 checksum, which some S3-compatible endpoints (not
        # verified either way for Railway's buckets) reject. "when_required"
        # is the standard compatibility fallback — harmless where checksums
        # are supported, and RECORDER-RAILWAY.md names this as the first
        # thing to check if uploads fail.
        config=BotoConfig(s3={"addressing_style": config.addressing_style},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
    )


class UploadError(Exception):
    """Wraps an S3 client failure for logging without secrets. `code` is
    always the error code/class only — never the original exception's
    message, which (per boto3/botocore) can include request details."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _error_code(exc):
    """The botocore `Error.Code` if this looks like a ClientError, else the
    exception's class name. Never the exception's message/args — those can
    echo request/response internals (see UploadError)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return code
    return type(exc).__name__


# ── upload ledger: which local files are confirmed uploaded ────────────────

class UploadLedger:
    """Per-file upload confirmation state, persisted as JSON under DATA_DIR
    with atomic writes (matches recorder.py's own write_status pattern), so
    it survives a restart. Keyed by the file's relative path under DATA_DIR
    (recorder.py writes files flat, so this is just the basename)."""

    def __init__(self, path):
        self._path = path
        self._entries = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    self._entries = json.load(f)
            except (OSError, ValueError) as exc:
                # Safe to start empty: HEAD-before-PUT re-confirms uploaded files.
                logger.warning("upload ledger unreadable (%s); starting empty", type(exc).__name__)
                self._entries = {}

    def _save(self):
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._entries, f, indent=2)
        os.replace(tmp, self._path)

    def confirmed_size(self, rel_path):
        """The size the ledger confirmed for `rel_path`, or None if it was
        never confirmed. Callers must compare this against the file's
        CURRENT size — recorder.py can append a second gzip member to an
        already-archived day if the wall clock steps back into it, so a
        stale confirmation must not authorize deleting bytes never
        uploaded."""
        with self._lock:
            entry = self._entries.get(rel_path)
            return entry.get("size") if entry and entry.get("confirmed") else None

    def mark_confirmed(self, rel_path, size, now_ms):
        with self._lock:
            self._entries[rel_path] = {"confirmed": True, "size": size, "confirmed_at_ms": now_ms}
            self._save()

    def mark_failed(self, rel_path, now_ms, reason):
        """`reason` must always be a static string / error code — never an
        exception's raw message, which could in principle echo
        request/response internals."""
        with self._lock:
            entry = self._entries.get(rel_path) or {"confirmed": False}
            entry["confirmed"] = False
            entry["last_attempt_ms"] = now_ms
            entry["last_error"] = reason
            self._entries[rel_path] = entry
            self._save()


# ── uploader: background thread, finished-day files only ───────────────────

def _day_from_finished_path(path):
    """Extract the `YYYY-MM-DD` day from a `<coin>_<channel>_<day>.jsonl.gz`
    path. None if it doesn't look like a finished day file."""
    name = os.path.basename(path)
    if not name.endswith(".jsonl.gz"):
        return None
    return name[:-len(".jsonl.gz")].rsplit("_", 1)[-1]


def _utc_day_end_ms(day_str):
    """Epoch ms at which the given UTC calendar day ended (= the start of
    the next UTC day). None if `day_str` doesn't parse."""
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000) + 86_400_000


class Uploader:
    """Background thread: finds finished (`.gz`) day files under `out_dir`
    not yet confirmed uploaded, uploads them to the S3-compatible bucket,
    confirms each with a follow-up HEAD, and deletes local copies once
    confirmed and older than `keep_days`. A network failure here never stops
    or slows the recorder's own capture loop — this runs on its own thread
    and only ever reads/deletes files the recorder isn't currently writing
    (finished `.gz` files only, never today's open plain file).

    `client` is None when S3 config is missing — uploads are then disabled
    (see `enabled`), and every pass is a no-op; recording continues either
    way. HEAD is always checked before any PUT: an existing key with a
    matching size counts as confirmed without uploading again; a mismatched
    size is a failure that is never overwritten."""

    def __init__(self, out_dir, bucket, client, *, keep_days=DEFAULT_KEEP_DAYS,
                retry_s=UPLOAD_RETRY_S, ledger=None, clock=lambda: int(time.time() * 1000)):
        self.out_dir = out_dir
        self.bucket = bucket
        self.client = client
        self.enabled = client is not None and bucket is not None
        self.keep_days = keep_days
        self.retry_s = retry_s
        self.ledger = ledger or UploadLedger(os.path.join(out_dir, "upload_ledger.json"))
        self.clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_success_ms = None
        self._failing_since_ms = None

    # -- candidate files -----------------------------------------------

    def _candidate_files(self):
        """Finished (`.gz`) day files under out_dir, excluding today's UTC
        day defensively. Never `.jsonl` — the current day's file is always
        plain (recorder.py never gzips it while open), so this glob
        structurally excludes both the open file and any uncompressed
        file."""
        today = rec.day_str_utc(self.clock())
        out = []
        for path in sorted(glob.glob(os.path.join(self.out_dir, "*.jsonl.gz"))):
            day = _day_from_finished_path(path)
            if day is None or day == today:
                continue
            out.append(path)
        return out

    def _rel_path(self, abs_path):
        return os.path.relpath(abs_path, self.out_dir)

    def _is_confirmed(self, path, rel):
        confirmed_size = self.ledger.confirmed_size(rel)
        if confirmed_size is None:
            return False
        try:
            current_size = os.path.getsize(path)
        except OSError:
            return False
        # A mismatch (e.g. recorder.py appended a second gzip member after a
        # clock step-back into an already-archived day) means the confirmed
        # bytes no longer match what's on disk — treat as unconfirmed so it
        # re-uploads rather than silently deleting data never uploaded.
        return current_size == confirmed_size

    # -- one pass over all pending files ------------------------------------

    def _pass_once(self):
        if not self.enabled:
            return
        any_pending = False
        all_ok = True
        for path in self._candidate_files():
            rel = self._rel_path(path)
            if self._is_confirmed(path, rel):
                continue
            any_pending = True
            if not self._upload_one(path, rel):
                all_ok = False
        with self._lock:
            if any_pending and not all_ok:
                if self._failing_since_ms is None:
                    self._failing_since_ms = self.clock()
            else:
                self._failing_since_ms = None
        self._maybe_prune()

    def _head(self, key):
        """Returns the object's ContentLength, or None if it's absent (a
        definite not-found HEAD response). Raises UploadError for anything
        else — an inconclusive read (network failure, 403, ...) must never
        be treated as "absent" and lead to a PUT that could overwrite."""
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = _error_code(exc)
            if code in NOT_FOUND_CODES:
                return None
            raise UploadError(code) from None
        return resp.get("ContentLength")

    def _upload_one(self, path, rel):
        now = self.clock()
        try:
            local_size = os.path.getsize(path)
        except OSError:
            logger.exception("could not stat %s for upload", rel)
            self.ledger.mark_failed(rel, now, "local-stat-error")
            return False

        try:
            remote_size = self._head(rel)
        except UploadError as exc:
            logger.warning("recorder upload HEAD failed for %s: %s", rel, exc.code)
            self.ledger.mark_failed(rel, now, exc.code)
            return False

        if remote_size is not None:
            if remote_size == local_size:
                self.ledger.mark_confirmed(rel, local_size, now)
                with self._lock:
                    self._last_success_ms = now
                return True
            logger.warning("recorder upload size mismatch for %s — never overwriting", rel)
            self.ledger.mark_failed(rel, now, "size-mismatch")
            return False

        try:
            with open(path, "rb") as f:
                self.client.put_object(Bucket=self.bucket, Key=rel, Body=f)
        except Exception as exc:
            code = _error_code(exc)
            logger.warning("recorder upload PUT failed for %s: %s", rel, code)
            self.ledger.mark_failed(rel, now, code)
            return False

        try:
            confirmed_size = self._head(rel)
        except UploadError as exc:
            logger.warning("recorder upload post-PUT HEAD failed for %s: %s", rel, exc.code)
            self.ledger.mark_failed(rel, now, exc.code)
            return False

        if confirmed_size != local_size:
            logger.warning("recorder upload not confirmed for %s after PUT", rel)
            self.ledger.mark_failed(rel, now, "post-upload-size-mismatch")
            return False

        self.ledger.mark_confirmed(rel, local_size, now)
        with self._lock:
            self._last_success_ms = now
        return True

    # -- pruning: delete only confirmed + past the keep window --------------

    def _maybe_prune(self):
        now_ms = self.clock()
        for path in self._candidate_files():
            rel = self._rel_path(path)
            if not self._is_confirmed(path, rel):
                continue
            day = _day_from_finished_path(path)
            day_end_ms = _utc_day_end_ms(day) if day else None
            if day_end_ms is None:
                continue
            age_days = (now_ms - day_end_ms) / 86_400_000
            if age_days > self.keep_days:
                try:
                    os.remove(path)
                    logger.info("removed confirmed-uploaded local file %s (%.1fd past its day)",
                                rel, age_days)
                except OSError:
                    logger.exception("could not remove confirmed local file %s", rel)

    # -- status (for /status; no secrets) -----------------------------------

    def status(self):
        now_ms = self.clock()
        oldest_day = None
        for path in self._candidate_files():
            rel = self._rel_path(path)
            if self._is_confirmed(path, rel):
                continue
            day = _day_from_finished_path(path)
            if day is not None and (oldest_day is None or day < oldest_day):
                oldest_day = day
        day_end_ms = _utc_day_end_ms(oldest_day) if oldest_day else None
        with self._lock:
            last_success_ms = self._last_success_ms
            failing_since_ms = self._failing_since_ms
        return {
            "uploads_enabled": self.enabled,
            "last_successful_upload_ts_utc": rec.iso_utc_ms(last_success_ms) if last_success_ms else None,
            "oldest_unconfirmed_day": oldest_day,
            "seconds_since_oldest_unconfirmed_day_ended": (
                round((now_ms - day_end_ms) / 1000, 1) if day_end_ms is not None else None),
            "upload_failing_since_ts_utc": (
                rec.iso_utc_ms(failing_since_ms) if failing_since_ms else None),
            "seconds_upload_failing": (
                round((now_ms - failing_since_ms) / 1000, 1) if failing_since_ms is not None else None),
        }

    # -- thread lifecycle -----------------------------------------------

    def run_forever(self):
        while not self._stop.is_set():
            try:
                self._pass_once()
            except Exception:
                logger.exception("recorder uploader pass raised — retrying next interval")
            self._stop.wait(self.retry_s)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True, name="uploader").start()

    def stop(self):
        self._stop.set()


# ── recorder status snapshot, read off the recorder's own loop thread ──────

class StatusSnapshot:
    """Holds the most recent status dict `FlowRecorder.on_status` handed us,
    with the wall-clock time it was taken. Read from the HTTP server thread;
    written from the recorder's asyncio loop thread. A lock guards the pair
    so a reader never sees a status from one moment paired with a
    timestamp from another."""

    def __init__(self, clock=lambda: int(time.time() * 1000)):
        self._lock = threading.Lock()
        self._clock = clock
        self._status = None
        self._taken_at_ms = None

    def update(self, status):
        with self._lock:
            self._status = status
            self._taken_at_ms = self._clock()

    def snapshot(self):
        with self._lock:
            return self._status, self._taken_at_ms


def build_full_status(snapshot, uploader, *, status_interval_s=STATUS_INTERVAL_S, now_ms=None):
    """Merges the recorder's own status (via the on_status hook — never
    fetched by calling FlowRecorder.status() from this thread, which would
    race the recv loop) with the uploader's status. If no snapshot has
    landed yet, or it's gone stale (the recorder's status loop itself
    stalled), reads as disconnected/silent rather than reporting nothing."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    rec_status, taken_at_ms = snapshot.snapshot()
    stale = (taken_at_ms is None
             or now_ms - taken_at_ms > status_interval_s * SNAPSHOT_STALE_MULTIPLE * 1000)
    if rec_status is None or stale:
        connected = False
        seconds_since_last_message = None
        reconnects = rec_status.get("reconnects_24h") if rec_status else None
        total_gap_seconds = rec_status.get("gap_seconds_24h") if rec_status else None
        disk_free_gb = rec_status.get("disk_free_gb") if rec_status else None
    else:
        connected = rec_status.get("connected", False)
        lag_s = rec_status.get("lag_s")
        snapshot_age_s = (now_ms - taken_at_ms) / 1000
        # The snapshot's own lag_s stops advancing the moment it was taken;
        # add the time since so a wedged-but-not-yet-stale loop still shows
        # growing silence instead of a frozen, falsely-fresh number.
        seconds_since_last_message = round(lag_s + snapshot_age_s, 1) if lag_s is not None else None
        reconnects = rec_status.get("reconnects_24h")
        total_gap_seconds = rec_status.get("gap_seconds_24h")
        disk_free_gb = rec_status.get("disk_free_gb")

    merged = {
        "connected": connected,
        "seconds_since_last_message": seconds_since_last_message,
        "reconnects": reconnects,
        "total_gap_seconds": total_gap_seconds,
        "disk_free_gb": disk_free_gb,
    }
    merged.update(uploader.status())
    return merged


# ── HTTP: /health, /status — stdlib only, no secrets, dual-stack ───────────

class DualStackHTTPServer(ThreadingHTTPServer):
    """Binds `::` so the service is reachable over Railway's IPv6 private
    network, while IPV6_V6ONLY=0 keeps it reachable over IPv4 too (dual
    stack) for local/manual checks."""

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass  # platform without IPV6_V6ONLY — IPv6-only is still correct
        super().server_bind()


def make_handler(snapshot, uploader):
    class RecordHandler(BaseHTTPRequestHandler):
        server_version = "railway-record/1"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

        def _respond(self, code, body=b"", content_type="text/plain"):
            self.send_response(code)
            if body:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            # Process-alive only — never tied to WS connectivity or upload
            # health, or Railway's own healthcheck would restart the
            # container during an ordinary exchange outage and manufacture
            # a recording gap. /status is where connectivity is reported.
            if self.path == "/health":
                self._respond(200, b"ok")
                return
            if self.path == "/status":
                body = json.dumps(build_full_status(snapshot, uploader), allow_nan=False).encode("utf-8")
                self._respond(200, body, content_type="application/json")
                return
            self._respond(404)

    return RecordHandler


# ── config from env, boot ───────────────────────────────────────────────

def parse_env(env=os.environ):
    """Pure parse of the env vars this service reads — unit-testable without
    starting any thread or socket. Never raises: missing S3 config means
    uploads are disabled (`s3_config` is None), not a fatal startup error —
    recording must continue regardless (see Uploader)."""
    coins = [c.strip().upper() for c in env.get("RECORDER_COINS", ",".join(DEFAULT_COINS)).split(",")
             if c.strip()]
    return {
        "coins": coins,
        "out_dir": env.get("DATA_DIR", "/data"),
        "port": int(env.get("PORT", "8080")),
        "keep_days": int(env.get("RECORDER_KEEP_DAYS", str(DEFAULT_KEEP_DAYS))),
        "s3_config": s3_config_from_env(env),
    }


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = parse_env()

    snapshot = StatusSnapshot()
    fr = rec.FlowRecorder(config["coins"], out_dir=config["out_dir"],
                          status_interval_s=STATUS_INTERVAL_S, on_status=snapshot.update)

    s3_config = config["s3_config"]
    if s3_config is None:
        logger.warning("S3 config incomplete; uploads disabled, recording continues")
        client, bucket = None, None
    else:
        client, bucket = make_real_s3_client(s3_config), s3_config.bucket
    uploader = Uploader(config["out_dir"], bucket, client, keep_days=config["keep_days"])
    uploader.start()

    server = DualStackHTTPServer(("::", config["port"]), make_handler(snapshot, uploader))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("recorder http server listening on :%d", config["port"])

    try:
        asyncio.run(fr.run())
    finally:
        server.shutdown()
        uploader.stop()


def main():
    try:
        run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
