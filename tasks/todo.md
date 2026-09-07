# Account protection and scalp integrity

## Objective

Prioritize observation of actual account losses and protective orders. Deliver a read-only account monitor, followed by focused fixes for confirmed signal and performance-reporting defects. Keep unvalidated entry research paper-only. No order placement, cancellation, position changes, messages to others, or automatic merging are authorized.

## Account monitor

- Read the configured public trading account from the exchange, including positions, equity, fills, funding, transfers and open orders. Cover configured perpetual venues and disclose incomplete coverage.
- Use an explicit daily loss limit selected by the user and a Singapore-midnight reset unless configured otherwise. Report missing configuration honestly.
- Persist a baseline with clear observation time. Do not invent a midnight equity value when starting later in the day. Bootstrap historical accounting only where source coverage can be verified.
- Separate realized trading results from transfer-adjusted equity loss including unrealized positions. Reconcile timestamps and expose missing or stale data.
- Report whether each position has an active, correctly directed protective stop with enough remaining size. Distinguish order presence from guaranteed execution and read-only monitoring from enforcement.
- Latch a triggered daily warning until the next configured day. A data gap or restart must not clear an existing breach.
- Expose concise human-readable output and machine-readable output for the scalp preflight. Missing data cannot authorize a new entry. Management of existing positions remains available.

## Integrity fixes, in a separate pull request

- Sparse recent-trade samples must not claim complete capture. Unknown or stale flow cannot confirm an entry.
- Net reward/risk includes after-cost loss; position sizing includes modeled execution costs.
- Missing required confirmation or low conviction produces WAIT. No changes intended to increase trading frequency.
- Replay best-alternative results are hypothetical, with actual, paper and counterfactual outcomes separated. Entry statistics must not count management rows as independent entries.
- Update the skill instructions and examples to match behavior without claiming profitability or validated historical savings.

## Validation

- Exercise account fixtures covering losses while positions remain open, partial fills and stops, invalid prices/sizes, multiple venues, transfers, funding, midnight rollover, restart, stale data and upstream failure.
- Reproduce each confirmed integrity defect, run focused tests and the full existing suite, and check source syntax and applicable lint/type checks.
- Inspect the committed diffs independently for correctness and conformance before opening reviewable pull requests. Keep any unresolved user-selected account settings explicit. Never merge automatically.

## Research boundary

The later experiment requires a continuous trade feed and a frozen hypothesis with identical baseline opportunities, realistic execution and untouched chronological evaluation data. It remains paper-only; repaired calculations and green software tests do not establish profitable signals.
