# 0001. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-07

## Context

Architectural decisions need a durable record of why a boundary or trade-off
was chosen; code alone cannot recover the rejected alternatives.

## Decision

Record repo-local decisions as numbered, dated Markdown files in this directory,
using Context, Decision and Consequences. Cross-project context remains in the wiki.

## Consequences

Write records only for meaningful decisions, not every commit. Supersede old
decisions with new records rather than deleting their history.
