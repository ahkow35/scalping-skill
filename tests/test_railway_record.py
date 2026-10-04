import datetime as dt
import hashlib
import json
import os
import time

import pytest

import railway_record as rr


BUCKET = "hype-tape"


# ── fakes: a fake S3 client only, no network, no boto3 required ───────────

class FakeClientError(Exception):
    """Mimics botocore.exceptions.ClientError's shape (a `.response` dict
    with `Error.Code`) without importing botocore — tests must run whether
    or not boto3 is installed locally."""

    def __init__(self, code, message="fake client error"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    """Models just enough of boto3's S3 client surface: head_object and
    put_object, with `objects` as the source of truth for what's "in the
    bucket". `fail_head_for` / `fail_put_for` inject a non-not-found error
    (a real network/permission failure) for one call."""

    def __init__(self):
        self.objects = {}       # key -> bytes
        self.metadata = {}      # key -> user metadata dict (as S3 returns it)
        self.fail_head_for = {}  # key -> exception to raise once
        self.fail_put_for = {}   # key -> exception to raise once
        self.head_calls = []
        self.put_calls = []

    def head_object(self, Bucket, Key):
        self.head_calls.append(Key)
        if Key in self.fail_head_for:
            raise self.fail_head_for.pop(Key)
        if Key not in self.objects:
            raise FakeClientError("404", "Not Found")
        return {"ContentLength": len(self.objects[Key]), "Metadata": dict(self.metadata.get(Key, {}))}

    def put_object(self, Bucket, Key, Body, Metadata=None):
        self.put_calls.append(Key)
        if Key in self.fail_put_for:
            raise self.fail_put_for.pop(Key)
        data = Body.read() if hasattr(Body, "read") else Body
        self.objects[Key] = data
        self.metadata[Key] = dict(Metadata or {})
        return {}

    def put_as_uploaded(self, key, content):
        """An object as a previous run of this uploader would have left it."""
        self.objects[key] = content
        self.metadata[key] = {"sha256": hashlib.sha256(content).hexdigest()}


def _finished(tmp_path, name, content=b"hello"):
    (tmp_path / name).write_bytes(content)
    return str(tmp_path / name)


def _uploader(tmp_path, client, **kwargs):
    kwargs.setdefault("clock", lambda: 2_000_000_000_000)  # far future -> any day is "finished"
    return rr.Uploader(str(tmp_path), BUCKET, client, **kwargs)


# ── small pure helpers ───────────────────────────────────────────────────

def test_day_from_finished_path():
    assert rr._day_from_finished_path("/data/HYPE_trades_2026-09-01.jsonl.gz") == "2026-09-01"
    assert rr._day_from_finished_path("/data/HYPE_trades_2026-09-01.jsonl") is None
    assert rr._day_from_finished_path("/data/status.json") is None


def test_utc_day_end_ms_is_start_of_next_utc_day():
    expected = int(dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert rr._utc_day_end_ms("2026-09-01") == expected
    assert rr._utc_day_end_ms("not-a-day") is None


def test_error_code_prefers_botocore_style_error_code():
    exc = FakeClientError("AccessDenied", "nope")
    assert rr._error_code(exc) == "AccessDenied"


def test_error_code_falls_back_to_exception_class_name():
    assert rr._error_code(ConnectionError("network is down")) == "ConnectionError"


# ── S3 config parsing: never raises; missing config just disables uploads ──

def test_s3_config_from_env_all_present():
    env = {"S3_ENDPOINT": "https://x.example", "S3_BUCKET": BUCKET,
           "S3_ACCESS_KEY_ID": "AKID", "S3_SECRET_ACCESS_KEY": "SECRET"}
    cfg = rr.s3_config_from_env(env)
    assert cfg.endpoint == "https://x.example"
    assert cfg.bucket == BUCKET
    assert cfg.region == "auto"
    assert cfg.addressing_style == "virtual"


def test_s3_config_from_env_honors_region_and_addressing_style_overrides():
    env = {"S3_ENDPOINT": "https://x.example", "S3_BUCKET": BUCKET,
           "S3_ACCESS_KEY_ID": "AKID", "S3_SECRET_ACCESS_KEY": "SECRET",
           "S3_REGION": "us-east-1", "S3_ADDRESSING_STYLE": "path"}
    cfg = rr.s3_config_from_env(env)
    assert cfg.region == "us-east-1"
    assert cfg.addressing_style == "path"


@pytest.mark.parametrize("missing", ["S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"])
def test_s3_config_from_env_missing_any_var_disables_uploads(missing):
    env = {"S3_ENDPOINT": "https://x.example", "S3_BUCKET": BUCKET,
           "S3_ACCESS_KEY_ID": "AKID", "S3_SECRET_ACCESS_KEY": "SECRET"}
    del env[missing]
    assert rr.s3_config_from_env(env) is None  # never raises


def test_s3_config_repr_never_includes_the_secret_or_access_key():
    cfg = rr.s3_config_from_env({"S3_ENDPOINT": "https://x.example", "S3_BUCKET": BUCKET,
                                  "S3_ACCESS_KEY_ID": "AKID-VALUE", "S3_SECRET_ACCESS_KEY": "SECRET-VALUE"})
    text = repr(cfg)
    assert "AKID-VALUE" not in text
    assert "SECRET-VALUE" not in text


def test_make_real_s3_client_does_not_require_boto3_at_import_time():
    # The lazy import lives inside make_real_s3_client itself — importing
    # railway_record (done at module load for this whole test file) must
    # never require boto3 to be installed.
    assert callable(rr.make_real_s3_client)


# ── upload + HEAD confirm by size ───────────────────────────────────────

def test_upload_confirmed_by_head_after_put(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    _finished(tmp_path, rel, b"abcdef")
    up = _uploader(tmp_path, client)
    up._pass_once()
    assert up.ledger.confirmed_size(rel, up.destination) == 6
    assert client.put_calls == [rel]
    assert client.head_calls == [rel, rel]  # pre-PUT HEAD (absent) then post-PUT HEAD (confirm)


def test_existing_key_with_matching_size_confirmed_without_put(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    content = b"abcdef"
    client.put_as_uploaded(rel, content)  # already present remotely, byte-identical
    _finished(tmp_path, rel, content)
    up = _uploader(tmp_path, client)
    up._pass_once()
    assert up.ledger.confirmed_size(rel, up.destination) == len(content)
    assert client.put_calls == []  # never uploaded again


def test_same_size_but_different_bytes_is_a_failure_never_overwritten(tmp_path):
    # Codex r1: equal size is not proof of equal bytes. The sha256 metadata
    # written at PUT time must match too.
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    client.put_as_uploaded(rel, b"zzzzzz")
    path = _finished(tmp_path, rel, b"abcdef")  # same length, different bytes
    up = _uploader(tmp_path, client)
    assert up._upload_one(path, rel) is False
    assert up.ledger.confirmed_size(rel, up.destination) is None
    assert client.objects[rel] == b"zzzzzz"
    assert client.put_calls == []


def test_existing_object_without_sha_metadata_is_never_trusted_or_overwritten(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    client.objects[rel] = b"abcdef"  # right bytes, but no digest to prove it
    path = _finished(tmp_path, rel, b"abcdef")
    up = _uploader(tmp_path, client)
    assert up._upload_one(path, rel) is False
    assert client.put_calls == []
    assert up.ledger.confirmed_size(rel, up.destination) is None


def test_put_is_confirmed_only_when_head_returns_the_digest(tmp_path):
    class DropsMetadataClient(FakeS3Client):
        def put_object(self, Bucket, Key, Body, Metadata=None):
            return super().put_object(Bucket=Bucket, Key=Key, Body=Body, Metadata=None)

    client = DropsMetadataClient()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    path = _finished(tmp_path, rel, b"abcdef")
    up = _uploader(tmp_path, client)
    assert up._upload_one(path, rel) is False
    assert up.ledger.confirmed_size(rel, up.destination) is None


def test_put_sends_the_files_sha256_as_metadata(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    path = _finished(tmp_path, rel, b"abcdef")
    _uploader(tmp_path, client)._upload_one(path, rel)
    assert client.metadata[rel] == {"sha256": hashlib.sha256(b"abcdef").hexdigest()}


def test_mismatched_size_is_a_failure_never_overwritten(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    remote_content = b"different-remote-bytes"
    client.objects[rel] = remote_content
    local_content = b"abcdef"
    path = _finished(tmp_path, rel, local_content)
    up = _uploader(tmp_path, client)
    ok = up._upload_one(path, rel)
    assert ok is False
    assert up.ledger.confirmed_size(rel, up.destination) is None
    assert client.objects[rel] == remote_content   # never overwritten remotely
    assert client.put_calls == []                  # PUT never attempted
    assert os.path.exists(path)                     # never deleted locally


def test_inconclusive_head_error_never_leads_to_a_put(tmp_path):
    # A 403 (e.g. missing ListBucket) or a network error on HEAD is not
    # "absent" — it must never be treated as license to PUT.
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    client.fail_head_for[rel] = FakeClientError("AccessDenied", "no ListBucket")
    _finished(tmp_path, rel, b"abc")
    up = _uploader(tmp_path, client)
    ok = up._upload_one(str(tmp_path / rel), rel)
    assert ok is False
    assert client.put_calls == []
    assert up.ledger.confirmed_size(rel, up.destination) is None


def test_retry_after_failure_succeeds_once_the_network_recovers(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    client.fail_head_for[rel] = ConnectionError("network is down")
    content = b"xyz"
    _finished(tmp_path, rel, content)
    up = _uploader(tmp_path, client)
    up._pass_once()
    assert up.ledger.confirmed_size(rel, up.destination) is None

    up._pass_once()  # the one-shot fail_head_for entry was consumed; this pass succeeds
    assert up.ledger.confirmed_size(rel, up.destination) == len(content)


def test_never_uploads_the_current_days_plain_file(tmp_path):
    client = FakeS3Client()
    (tmp_path / "HYPE_trades_2026-09-28.jsonl").write_bytes(b"today, still open")
    today_ms = 1_790_000_000_000  # any fixed instant; the plain file is never a .gz candidate anyway
    up = _uploader(tmp_path, client, clock=lambda: today_ms)
    up._pass_once()
    assert client.put_calls == []
    assert client.head_calls == []


def test_never_uploads_todays_own_gz_defensively(tmp_path):
    # Defensive: even a .gz whose day-string equals "today" per the
    # uploader's own clock is excluded (recorder.py never produces one in
    # practice — today's file always stays plain — but the exclusion is
    # cheap insurance).
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    _finished(tmp_path, rel, b"abc")
    today_ms = int(dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc).timestamp() * 1000)
    up = _uploader(tmp_path, client, clock=lambda: today_ms)
    up._pass_once()
    assert client.put_calls == []


def test_confirmed_ledger_entry_invalidated_when_local_file_grows(tmp_path):
    # Regression: recorder.py documents appending a second gzip member to an
    # already-archived day if the wall clock steps back into it. A stale
    # ledger "confirmed" at the old size must not authorize deleting bytes
    # that were never uploaded.
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    path = _finished(tmp_path, rel, b"abc")
    up = _uploader(tmp_path, client)
    up._pass_once()
    assert up.ledger.confirmed_size(rel, up.destination) == 3
    original_remote = client.objects[rel]

    with open(path, "ab") as f:
        f.write(b"more-bytes-appended-locally")
    assert up._is_confirmed(path, rel) is False

    up._pass_once()
    assert up.ledger.confirmed_size(rel, up.destination) is None
    assert client.objects[rel] == original_remote
    assert os.path.exists(path)


# ── missing S3 config disables uploads without stopping recording ────────

def test_disabled_uploader_never_calls_the_client_and_recording_continues(tmp_path):
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    _finished(tmp_path, rel, b"data")
    up = rr.Uploader(str(tmp_path), None, None, clock=lambda: 2_000_000_000_000)
    assert up.enabled is False
    up._pass_once()  # must not raise despite client=None
    assert up.ledger.confirmed_size(rel, up.destination) is None
    st = up.status()
    assert st["uploads_enabled"] is False


# ── deletion: only confirmed + past the keep window ─────────────────────

def test_deletion_only_after_confirmed_and_past_keep_window(tmp_path):
    client = FakeS3Client()
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    _finished(tmp_path, rel, b"data")
    day_end_ms = rr._utc_day_end_ms("2026-09-01")
    clock = {"t": day_end_ms + 1 * 86_400_000}  # 1 day past day-end, keep_days default 3
    up = _uploader(tmp_path, client, clock=lambda: clock["t"], keep_days=3)
    up._pass_once()
    assert os.path.exists(os.path.join(str(tmp_path), rel))  # confirmed but inside the keep window

    clock["t"] = day_end_ms + 3 * 86_400_000  # exactly at the boundary — "more than" keep_days, not yet
    up._pass_once()
    assert os.path.exists(os.path.join(str(tmp_path), rel))

    clock["t"] = day_end_ms + 4 * 86_400_000  # now past the keep window
    up._pass_once()
    assert not os.path.exists(os.path.join(str(tmp_path), rel))


def test_never_deletes_an_unconfirmed_file_however_old(tmp_path):
    rel = "HYPE_trades_2026-01-01.jsonl.gz"
    _finished(tmp_path, rel, b"data")
    far_future = rr._utc_day_end_ms("2026-01-01") + 365 * 86_400_000

    class AlwaysFailClient(FakeS3Client):
        def head_object(self, Bucket, Key):
            self.head_calls.append(Key)
            raise FakeClientError("AccessDenied", "nope")

    up = _uploader(tmp_path, AlwaysFailClient(), clock=lambda: far_future, keep_days=3)
    up._pass_once()
    assert os.path.exists(os.path.join(str(tmp_path), rel))


# ── ledger persistence ───────────────────────────────────────────────────

def test_ledger_survives_restart(tmp_path):
    path = str(tmp_path / "upload_ledger.json")
    ledger = rr.UploadLedger(path)
    ledger.mark_confirmed("f.jsonl.gz", 123, 1000, "https://s3.example|b")
    ledger_reloaded = rr.UploadLedger(path)
    assert ledger_reloaded.confirmed_size("f.jsonl.gz", "https://s3.example|b") == 123


def test_ledger_confirmation_does_not_carry_over_to_a_new_bucket(tmp_path):
    # Codex r1: moving to a new bucket must re-upload, never trust (and
    # prune on the strength of) a confirmation from the old destination.
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    content = b"abc"
    _finished(tmp_path, rel, content)
    inside_keep_window = rr._utc_day_end_ms("2026-09-01") + 3_600_000  # so nothing is pruned
    old = _uploader(tmp_path, FakeS3Client(), destination="https://s3.example|old",
                    clock=lambda: inside_keep_window)
    old._pass_once()
    assert old.ledger.confirmed_size(rel, old.destination) == len(content)

    new_client = FakeS3Client()
    new = _uploader(tmp_path, new_client, destination="https://s3.example|new",
                    clock=lambda: inside_keep_window)
    assert new._is_confirmed(str(tmp_path / rel), rel) is False
    new._pass_once()
    assert new_client.put_calls == [rel]
    assert new.ledger.confirmed_size(rel, new.destination) == len(content)


def test_uploader_restart_with_a_confirming_ledger_never_touches_the_client(tmp_path):
    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    content = b"abc"
    _finished(tmp_path, rel, content)

    first = _uploader(tmp_path, FakeS3Client())
    first._pass_once()
    assert first.ledger.confirmed_size(rel, first.destination) == len(content)

    class AssertNeverCalledClient:
        def head_object(self, **kwargs):
            raise AssertionError("should not call the S3 client — ledger already confirmed")

        def put_object(self, **kwargs):
            raise AssertionError("should not call the S3 client — ledger already confirmed")

    second = _uploader(tmp_path, AssertNeverCalledClient())
    second._pass_once()  # no exception — the ledger alone was enough


# ── uploader status: oldest unconfirmed day, failing-since ──────────────

def test_uploader_status_tracks_oldest_unconfirmed_day_and_failing_since(tmp_path):
    class FailingClient(FakeS3Client):
        def __init__(self, fail_keys):
            super().__init__()
            self._fail_keys = set(fail_keys)

        def head_object(self, Bucket, Key):
            self.head_calls.append(Key)
            if Key in self._fail_keys:
                raise FakeClientError("AccessDenied", "nope")
            return super().head_object(Bucket=Bucket, Key=Key)

    rel_old = "HYPE_trades_2026-09-01.jsonl.gz"
    rel_new = "HYPE_trades_2026-09-03.jsonl.gz"
    client = FailingClient({rel_old, rel_new})
    _finished(tmp_path, rel_old, b"a")
    _finished(tmp_path, rel_new, b"b")
    start_ms = rr._utc_day_end_ms("2026-09-03") + 1000
    clock = {"t": start_ms}
    up = _uploader(tmp_path, client, clock=lambda: clock["t"])
    up._pass_once()               # first failing pass: failing_since_ms set to start_ms
    clock["t"] += 1000

    st = up.status()
    expected_day_end_gap_s = (clock["t"] - rr._utc_day_end_ms("2026-09-01")) / 1000
    assert st["uploads_enabled"] is True
    assert st["oldest_unconfirmed_day"] == "2026-09-01"
    assert st["upload_failing_since_ts_utc"] is not None
    assert st["seconds_upload_failing"] == 1.0
    assert st["seconds_since_oldest_unconfirmed_day_ended"] == expected_day_end_gap_s

    client._fail_keys.clear()
    clock["t"] += 5000
    up._pass_once()
    st2 = up.status()
    assert st2["oldest_unconfirmed_day"] is None
    assert st2["upload_failing_since_ts_utc"] is None
    assert st2["last_successful_upload_ts_utc"] is not None


# ── /health, /status over the stdlib handler (no real socket bind) ──────

def test_health_and_status_endpoints():
    from test_railway_watch import invoke, status_line

    # build_full_status()'s do_GET call uses the real wall clock (no injected
    # now_ms in the HTTP path) — give the snapshot a matching real-time clock
    # so it reads as fresh rather than falsely stale against an unrelated
    # fixed timestamp.
    snapshot = rr.StatusSnapshot(clock=lambda: int(time.time() * 1000))
    snapshot.update({"connected": True, "lag_s": 1.0, "reconnects_24h": 0,
                      "gap_seconds_24h": 0.0, "disk_free_gb": 50.0})

    class FakeUploader:
        def status(self):
            return {"uploads_enabled": True,
                     "last_successful_upload_ts_utc": None, "oldest_unconfirmed_day": None,
                     "seconds_since_oldest_unconfirmed_day_ended": None,
                     "upload_failing_since_ts_utc": None, "seconds_upload_failing": None}

    handler = rr.make_handler(snapshot, FakeUploader())

    health = invoke(handler, b"GET /health HTTP/1.1\r\n\r\n")
    assert b"200" in status_line(health)
    assert health.endswith(b"ok")

    status = invoke(handler, b"GET /status HTTP/1.1\r\n\r\n")
    assert b"200" in status_line(status)
    payload = json.loads(status.split(b"\r\n\r\n", 1)[1])
    assert payload["connected"] is True
    assert payload["seconds_since_last_message"] is not None
    assert payload["uploads_enabled"] is True

    unknown = invoke(handler, b"GET /orders HTTP/1.1\r\n\r\n")
    assert b"404" in status_line(unknown)


def test_health_endpoint_is_always_200_even_when_disconnected():
    # /health is process-alive only, never tied to WS connectivity — see
    # make_handler's do_GET docstring/comment.
    from test_railway_watch import invoke, status_line

    snapshot = rr.StatusSnapshot(clock=lambda: 1000)  # never updated -> "stale"/disconnected

    class FakeUploader:
        def status(self):
            return {}

    handler = rr.make_handler(snapshot, FakeUploader())
    health = invoke(handler, b"GET /health HTTP/1.1\r\n\r\n")
    assert b"200" in status_line(health)


def test_status_body_carries_only_the_documented_safe_fields():
    snapshot = rr.StatusSnapshot(clock=lambda: 1000)
    snapshot.update({"connected": True, "lag_s": 1.0, "reconnects_24h": 0,
                      "gap_seconds_24h": 0.0, "disk_free_gb": 10.0})

    class FakeUploader:
        def status(self):
            return {"uploads_enabled": True,
                     "last_successful_upload_ts_utc": None, "oldest_unconfirmed_day": None,
                     "seconds_since_oldest_unconfirmed_day_ended": None,
                     "upload_failing_since_ts_utc": None, "seconds_upload_failing": None}

    body = rr.build_full_status(snapshot, FakeUploader(), now_ms=1000)
    assert set(body.keys()) == {
        "connected", "seconds_since_last_message", "reconnects", "total_gap_seconds",
        "disk_free_gb", "uploads_enabled", "last_successful_upload_ts_utc", "oldest_unconfirmed_day",
        "seconds_since_oldest_unconfirmed_day_ended", "upload_failing_since_ts_utc",
        "seconds_upload_failing",
    }
    # No S3 credential field name could plausibly leak in here either.
    for forbidden in ("access_key", "secret", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        assert forbidden not in body


# ── snapshot staleness (the recorder's own status loop stalling) ────────

def test_build_full_status_disconnected_when_no_snapshot_yet():
    snapshot = rr.StatusSnapshot(clock=lambda: 1000)

    class FakeUploader:
        def status(self):
            return {}

    body = rr.build_full_status(snapshot, FakeUploader(), now_ms=1000)
    assert body["connected"] is False
    assert body["seconds_since_last_message"] is None


def test_build_full_status_reads_silent_once_the_snapshot_goes_stale():
    snapshot = rr.StatusSnapshot(clock=lambda: 0)
    snapshot.update({"connected": True, "lag_s": 1.0, "reconnects_24h": 2,
                      "gap_seconds_24h": 5.0, "disk_free_gb": 10.0})

    class FakeUploader:
        def status(self):
            return {}

    stale_now = rr.STATUS_INTERVAL_S * rr.SNAPSHOT_STALE_MULTIPLE * 1000 + 1000
    body = rr.build_full_status(snapshot, FakeUploader(), now_ms=stale_now)
    assert body["connected"] is False
    assert body["seconds_since_last_message"] is None
    assert body["reconnects"] == 2  # non-liveness fields still pass through


def test_build_full_status_adds_snapshot_age_to_lag():
    snapshot = rr.StatusSnapshot(clock=lambda: 0)
    snapshot.update({"connected": True, "lag_s": 2.0, "reconnects_24h": 0,
                      "gap_seconds_24h": 0.0, "disk_free_gb": 1.0})

    class FakeUploader:
        def status(self):
            return {}

    body = rr.build_full_status(snapshot, FakeUploader(), now_ms=5000)  # 5s after the snapshot
    assert body["seconds_since_last_message"] == 7.0  # 2.0 lag + 5.0s since the snapshot was taken


# ── FlowRecorder.on_status hook ─────────────────────────────────────────

def test_on_status_hook_fires_with_the_computed_status(tmp_path):
    import recorder as rec

    seen = []
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: 1_700_000_000_000,
                          on_status=seen.append)
    st = fr.status()
    fr.on_status(st)
    assert seen == [st]
    assert seen[0]["connected"] is False  # never connected in this test — sanity check the shape


def test_on_status_hook_default_none_is_behaviour_preserving(tmp_path):
    import recorder as rec

    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path))
    assert fr.on_status is None
    fr.status()  # must not raise with no hook set


# ── env parsing: never raises; missing S3 config just disables uploads ───

def test_parse_env_defaults():
    cfg = rr.parse_env({})
    assert cfg["coins"] == ["HYPE"]
    assert cfg["out_dir"] == "/data"
    assert cfg["port"] == 8080
    assert cfg["keep_days"] == 3
    assert cfg["s3_config"] is None  # no S3 vars set — never raises


def test_parse_env_honors_overrides():
    env = {"RECORDER_COINS": "hype, btc", "DATA_DIR": "/mnt/vol", "PORT": "9090",
           "RECORDER_KEEP_DAYS": "7", "RECORDER_DISK_FLOOR_GB": "2",
           "S3_ENDPOINT": "https://x", "S3_BUCKET": BUCKET,
           "S3_ACCESS_KEY_ID": "AKID", "S3_SECRET_ACCESS_KEY": "SECRET"}
    cfg = rr.parse_env(env)
    assert cfg["coins"] == ["HYPE", "BTC"]
    assert cfg["out_dir"] == "/mnt/vol"
    assert cfg["port"] == 9090
    assert cfg["keep_days"] == 7
    assert cfg["disk_floor_gb"] == 2
    assert cfg["s3_config"].bucket == BUCKET


def test_parse_env_malformed_ints_fall_back_to_defaults():
    cfg = rr.parse_env({"PORT": "80a", "RECORDER_KEEP_DAYS": "-2", "RECORDER_DISK_FLOOR_GB": "2GB"})
    assert cfg["port"] == 8080
    assert cfg["keep_days"] == 3
    assert cfg["disk_floor_gb"] == rr.rec.DISK_FLOOR_GB


# ── client setup failure disables uploads, never stops recording ────────

def _s3_config():
    return rr.s3_config_from_env({"S3_ENDPOINT": "https://x.example", "S3_BUCKET": BUCKET,
                                  "S3_ACCESS_KEY_ID": "AKID", "S3_SECRET_ACCESS_KEY": "SECRET"})


def test_build_s3_client_missing_config_disables_uploads():
    assert rr.build_s3_client(None) == (None, None, None)


def test_build_s3_client_setup_error_disables_uploads_without_leaking(caplog):
    # Codex r1: a malformed endpoint used to raise out of run() and stop
    # recording too. Now it only disables uploads, and logs the class only.
    def boom(_config):
        raise ValueError("Invalid endpoint: SECRET-looking detail")
    assert rr.build_s3_client(_s3_config(), make_client=boom) == (None, None, None)
    assert "ValueError" in caplog.text
    assert "SECRET" not in caplog.text


def test_build_s3_client_binds_destination_to_endpoint_and_bucket():
    client, bucket, destination = rr.build_s3_client(_s3_config(), make_client=lambda c: "client")
    assert (client, bucket, destination) == ("client", BUCKET, f"https://x.example|{BUCKET}")


# ── secrets never leak, across HEAD/PUT failures including exceptions ────

def test_secrets_never_appear_in_logs_across_upload_failures(tmp_path, caplog):
    caplog.set_level("DEBUG")
    secret_key = "super-secret-access-key-abc123"
    access_key_id = "super-secret-access-key-id-xyz"

    class LeakyErrorClient:
        """An adversarial fake: every exception's message embeds the fake
        secret, the way a raw botocore request-detail dump might. Only
        `.response['Error']['Code']` (never the message) may ever be
        logged."""

        def head_object(self, Bucket, Key):
            exc = FakeClientError(
                "AccessDenied",
                f"request signed with access_key_id={access_key_id} secret={secret_key}")
            raise exc

        def put_object(self, Bucket, Key, Body):
            raise RuntimeError(f"network error while using secret={secret_key} key_id={access_key_id}")

    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    _finished(tmp_path, rel, b"data")
    up = _uploader(tmp_path, LeakyErrorClient())
    up._pass_once()

    log_text = caplog.text
    assert secret_key not in log_text
    assert access_key_id not in log_text
    # The ledger's own persisted failure reason must be secret-free too.
    ledger_text = (tmp_path / "upload_ledger.json").read_text()
    assert secret_key not in ledger_text
    assert access_key_id not in ledger_text


def test_secrets_never_appear_in_status_even_after_a_failure(tmp_path):
    secret_key = "another-super-secret-key-999"

    class LeakyClient:
        def head_object(self, Bucket, Key):
            raise FakeClientError("AccessDenied", f"leaked secret={secret_key}")

        def put_object(self, Bucket, Key, Body):
            raise RuntimeError(f"leaked secret={secret_key}")

    rel = "HYPE_trades_2026-09-01.jsonl.gz"
    _finished(tmp_path, rel, b"data")
    up = _uploader(tmp_path, LeakyClient())
    up._pass_once()
    status_text = json.dumps(up.status())
    assert secret_key not in status_text


# ── GET /flow?coin=X (no real socket bind) ────────────────────────────────

def _flow_handler(flow):
    class FakeUploader:
        def status(self):
            return {}
    return rr.make_handler(rr.StatusSnapshot(clock=lambda: 1000), FakeUploader(), flow)


def test_flow_endpoint_returns_the_tape_for_a_recorded_coin():
    from test_railway_watch import invoke, status_line

    seen = []

    def flow(coin):
        seen.append(coin)
        return {"coin": coin, "rows": [], "gaps": []}

    response = invoke(_flow_handler(flow), b"GET /flow?coin=hype HTTP/1.1\r\n\r\n")
    assert b"200" in status_line(response)
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["coin"] == "hype"
    assert seen == ["hype"]      # the handler does not guess a spelling; flow() resolves it


def test_resolve_flow_coin_is_case_insensitive_and_returns_the_configured_spelling():
    coins = ["HYPE", "ZEC", "xyz:GOLD"]
    assert rr.resolve_flow_coin(coins, "hype") == "HYPE"
    assert rr.resolve_flow_coin(coins, "XYZ:gold") == "xyz:GOLD"
    assert rr.resolve_flow_coin(coins, "DOGE") is None


def test_flow_endpoint_404_json_for_an_unrecorded_or_missing_coin():
    from test_railway_watch import invoke, status_line

    handler = _flow_handler(lambda coin: None)
    for target in (b"/flow?coin=DOGE", b"/flow"):
        response = invoke(handler, b"GET " + target + b" HTTP/1.1\r\n\r\n")
        assert b"404" in status_line(response)
        assert json.loads(response.split(b"\r\n\r\n", 1)[1])["error"] == "coin_not_recorded"


def test_flow_endpoint_is_a_404_when_no_flow_source_is_given():
    from test_railway_watch import invoke, status_line

    response = invoke(_flow_handler(None), b"GET /flow?coin=HYPE HTTP/1.1\r\n\r\n")
    assert b"404" in status_line(response)
