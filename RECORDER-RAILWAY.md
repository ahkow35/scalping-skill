# Flow recorder as a second Railway service

`railway_record.py` runs `recorder.py`'s WebSocket capture loop as its own
always-on Railway service, alongside `railway_watch.py` (ACCOUNT-MONITOR.md)
in the same Railway project. It uploads each finished UTC-day compressed
file to a private Railway Storage Bucket (S3-compatible) and serves a small
`/health` + `/status` HTTP address that the watcher can optionally poll for
alerts.

Read-only toward the exchange: it only subscribes to public market data
(trades, `l2Book`, `bbo`) over a WebSocket. It never places, cancels or
closes an order, and holds no trading credential.

## Owner setup (steps only the account owner can do)

### 1. Create a private Storage Bucket

In the Railway project (the one already hosting the watcher, PR A):
**New → Storage Bucket**. This gives you an S3-compatible bucket with its
own endpoint, region, bucket name, access key id and secret access key —
each exposed as a Railway variable on the bucket service.

### 2. Create the recorder service

- **New service → same GitHub repo** (this one).
- Settings → set the **custom config path** to `railway.recorder.json` (this
  points it at `Dockerfile.recorder` instead of the watcher's `Dockerfile`).
- Settings → Volumes → attach a volume at `/data`.
- This service needs **no public Railway domain** — the watcher reaches it
  over Railway's private network (`<service>.railway.internal`), which
  exists automatically without exposing a public URL.

### 3. Wire the bucket's credentials in as variable references

On the recorder service, add these five variables. Use Railway's variable
**reference** syntax (`${{StorageBucketServiceName.VARIABLE_NAME}}`) so the
actual secret only ever lives on the bucket service, never typed a second
time:

- `S3_ENDPOINT` — the bucket's endpoint.
- `S3_REGION` — the bucket's region (optional; defaults to `auto` if unset).
- `S3_BUCKET` — the bucket name.
- `S3_ACCESS_KEY_ID` — the bucket's access key id.
- `S3_SECRET_ACCESS_KEY` — the bucket's secret access key.

If uploads later fail: the first thing to check is a signing/addressing
error, fixed by setting `S3_ADDRESSING_STYLE=path` (default is `virtual`;
some older Railway buckets need path-style addressing). The service already
asks boto3 for `when_required` checksum behaviour rather than its newer
default (mandatory `aws-chunked` bodies with a trailing CRC32 checksum,
which some S3-compatible endpoints reject) — if uploads still fail with a
checksum-related error, that's the next thing to check against Railway's
own bucket docs.

Also set, optionally:

- `RECORDER_COINS` — comma-separated, default `HYPE`.
- `RECORDER_KEEP_DAYS` — default `3`.
- `DATA_DIR` — default `/data` (matches the attached volume).

**Missing any one of the five required `S3_*` variables does not stop the
service from starting or recording** — uploads are simply disabled, visible
in `/status` (`uploads_enabled: false`) and raised as an alert once the
watcher is wired below. This is deliberate: a recording gap is worse than a
temporary upload gap.

### 4. Point the watcher at it (optional — alerts only)

On the **watcher** service (not this one), set:

- `RECORDER_STATUS_URL` — `http://<recorder-service-name>.railway.internal:8080/status`
  (use the recorder service's actual Railway service name; the port matches
  the recorder's `PORT`, default `8080`). Leaving this unset keeps the
  watcher exactly as it was before this PR — the recorder check is entirely
  off by default.

Redeploy the watcher for the new variable to take effect (setting a Railway
variable does not itself trigger a redeploy).

## What it does

- Runs `recorder.py`'s existing capture loop unchanged — what and how it
  records is untouched by this PR.
- A background thread uploads each finished (already gzip-rotated) day file
  to `S3_BUCKET`, using HEAD-before-PUT semantics: an object already present
  with the same byte size counts as confirmed without re-uploading; a
  different size is treated as a failure and **never overwritten**; an
  absent object is PUT, then confirmed with a follow-up HEAD matching its
  size. Any failure retries every 10 minutes — recording itself is never
  slowed or stopped by a network or bucket problem.
- Deletes a local `.gz` only once its upload is confirmed **and** its UTC
  day is more than `RECORDER_KEEP_DAYS` days past its end. A local file that
  fails to upload is never deleted, however old.
- Serves `GET /health` (200, process-alive only, for Railway's own health
  check — never tied to exchange connectivity) and `GET /status` (JSON —
  connected, seconds since last message, reconnects, total gap seconds,
  last successful upload time, the oldest finished day not yet confirmed
  uploaded, how long uploads have been failing, and disk free — never a
  secret) on `PORT`, bound to `::` (Railway's private network is IPv6).
- If `RECORDER_STATUS_URL` is set on the watcher, it polls this `/status` at
  most once a minute and raises two alerts through the same Telegram
  channel and appear/repeat/clear rules as its own account problems:
  `RECORDER_SILENT` (unreachable, disconnected, or silent for 5 minutes) and
  `RECORDER_UPLOAD_FAILING` (uploads disabled by missing bucket
  configuration, a day unconfirmed, or uploads failing, for over 24 hours).
  Neither ever touches `/report`, `entry_allowed`, or any account-derived
  alert.

## Listing / downloading the tape later

The bucket is S3-compatible, so the standard `aws` CLI works against it with
the bucket's endpoint:

```
aws s3 --endpoint-url "$S3_ENDPOINT" ls "s3://$S3_BUCKET/"
aws s3 --endpoint-url "$S3_ENDPOINT" cp "s3://$S3_BUCKET/HYPE_trades_2026-09-01.jsonl.gz" .
```

(`aws configure` or the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env
vars need to hold the bucket's access key id and secret access key from
step 1 — never paste them into chat.)

## Costs (measured July sample)

About 13 KB/minute compressed for HYPE alone — roughly 20 MB/day, 0.6
GB/month.
