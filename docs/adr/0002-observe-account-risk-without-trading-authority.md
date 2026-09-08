# 0002. Observe account risk without trading authority

- **Status:** Proposed for review
- **Date:** 2026-09-07

## Context

At base revision `4aca996`, the behavioral gate reads the skill's manual audit
journal and cannot see bypassing manual trades. The requested priority is
account-wide loss awareness before further entry research. No authority to
place, cancel or close exchange orders was granted.

## Decision

Keep the exchange transport restricted to public information reads, separate
from pure observation validation and daily-risk evaluation. Persist an atomic,
per-wallet daily latch and invoke it as a new-entry preflight. Unsupported
accounting modes and missing data cannot authorize an entry. Never reconstruct
unobserved midnight equity from an assumption. Keep order coverage separate
from execution guarantees.

## Consequences

The monitor observes manually originated activity in its declared scope without
holding a signing key. It cannot enforce losses against manual trading or
provide a hard daily-loss guarantee. Shared-balance account modes remain
unsupported until separately reconciled. A future enforcement integration
requires explicit trading authority, its own design and independent review;
it is not an extension of this read-only permission.
