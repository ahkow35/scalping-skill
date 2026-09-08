"""Read-only Hyperliquid account observations. No signing or exchange endpoint.

The transport is deliberately restricted to documented /info reads. Equity
aggregation is supported only for standard, independently margined USDC perps;
other account modes still produce observations, never an inferred equity.
"""

import concurrent.futures
import http.client
import json
import re
import time
import urllib.error
import urllib.request


INFO_URL = "https://api.hyperliquid.xyz/info"
READ_TYPES = frozenset({
    "userRole", "userAbstraction", "perpDexs", "meta", "clearinghouseState",
    "spotClearinghouseState", "frontendOpenOrders", "userFillsByTime", "userFunding",
    "userNonFundingLedgerUpdates",
})
MAX_HISTORY_REQUESTS = 128


class AccountDataError(ValueError):
    """Missing or ambiguous account data; must not authorize a new entry."""


def validate_wallet(wallet):
    if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        raise AccountDataError("wallet must be the public 0x trading-account address")
    if int(wallet[2:], 16) == 0:
        raise AccountDataError("the zero address is not a trading account")
    return wallet.lower()


def post_info(payload):
    """Only public read methods are accepted, even if a caller is mistaken."""
    if payload.get("type") not in READ_TYPES:
        raise AccountDataError("not an allowed read-only account query")
    request = urllib.request.Request(
        INFO_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "scalp-account-monitor/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.load(response)
    except (OSError, urllib.error.URLError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise AccountDataError(f"{payload['type']} unavailable: {exc}") from exc


def fetch_history(kind, wallet, start_ms, end_ms, *, post=post_info):
    """Split saturated time ranges instead of skipping same-millisecond events.

    This cannot recover data pruned by the venue. The monitor separately checks
    the accounting identity and baseline freshness; returned rows are not proof
    of complete historical coverage. Saturated one-ms windows fail explicitly.
    """
    if kind not in {"userFillsByTime", "userFunding", "userNonFundingLedgerUpdates"}:
        raise AccountDataError("not a supported history query")
    cap = 2000 if kind == "userFillsByTime" else 500
    windows = [(int(start_ms), int(end_ms))]
    result, requests = {}, 0
    while windows:
        start, end = windows.pop()
        requests += 1
        if requests > MAX_HISTORY_REQUESTS:
            raise AccountDataError(f"{kind} history exceeds bounded request budget")
        payload = {"type": kind, "user": wallet, "startTime": start, "endTime": end}
        if kind == "userFillsByTime":
            payload["aggregateByTime"] = False
        rows = post(payload)
        if not isinstance(rows, list):
            raise AccountDataError(f"{kind} returned a non-list response")
        if len(rows) >= cap:
            if start >= end:
                raise AccountDataError(f"{kind} truncated at a single timestamp")
            middle = (start + end) // 2
            windows.extend([(middle + 1, end), (start, middle)])
            continue
        for row in rows:
            if not isinstance(row, dict):
                raise AccountDataError(f"{kind} malformed history row")
            try:
                stamp = int(row["time"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AccountDataError(f"{kind} missing event time") from exc
            if not start <= stamp <= end:
                raise AccountDataError(f"{kind} returned an event outside requested interval")
            # Exact duplicate payloads may recur in API pages. Conflicting fill
            # identities are rejected later instead of silently choosing one.
            result[json.dumps(row, sort_keys=True)] = row
    return sorted(result.values(), key=lambda row: int(row["time"]))


def fetch_snapshot(wallet, since_ms, *, post=post_info, clock=None):
    """Observe every advertised perp DEX for this named wallet (not subaccounts).

    All responses are retained for pure validation. A missing venue fails the
    whole observation instead of looking like zero exposure. Snapshot timestamps
    and the accounting identity catch reads that race a trade or transfer.
    """
    wallet = validate_wallet(wallet)
    clock = clock or (lambda: int(time.time() * 1000))
    started = clock()
    role = post({"type": "userRole", "user": wallet})
    if not isinstance(role, dict) or role.get("role") not in {"user", "subAccount"}:
        raise AccountDataError("address is not a trading user/subaccount; do not use an API-agent address")
    mode = post({"type": "userAbstraction", "user": wallet})
    spot = None
    if mode == "unifiedAccount":
        # Unified accounts hold their USDC in the spot clearinghouse; per-dex
        # perp balances are not meaningful there. Positions still come per dex.
        spot = post({"type": "spotClearinghouseState", "user": wallet})
    raw_dexes = post({"type": "perpDexs"})
    if not isinstance(raw_dexes, list) or not raw_dexes or raw_dexes[0] is not None:
        raise AccountDataError("perp DEX discovery unavailable or malformed")
    dexes = [""]
    for dex in raw_dexes[1:]:
        if not isinstance(dex, dict) or not isinstance(dex.get("name"), str) or not dex["name"]:
            raise AccountDataError("malformed perp DEX name")
        if dex["name"] in dexes:
            raise AccountDataError("duplicate perp DEX name")
        dexes.append(dex["name"])

    def venue(dex):
        state = post({"type": "clearinghouseState", "user": wallet, "dex": dex})
        orders = post({"type": "frontendOpenOrders", "user": wallet, "dex": dex})
        # Core perps are USDC collateralized. Query HIP-3 metadata only for
        # nonempty venues; polling every unused builder's metadata is expensive.
        try:
            active = bool(state["assetPositions"] or orders
                          or float(state["marginSummary"]["accountValue"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AccountDataError("malformed venue balance") from exc
        meta = {"collateralToken": 0} if not dex else {"collateralToken": None}
        if dex and active:
            meta = post({"type": "meta", "dex": dex})
        return dex, {"state": state, "orders": orders, "meta": meta}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        venues = dict(pool.map(venue, dexes))
    try:
        # History ends at the latest observed account timestamp, not a later
        # wall-clock time that might include fills absent from these balances.
        asof = max(int(value["state"]["time"]) for value in venues.values())
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountDataError("account snapshot has no exchange timestamp") from exc
    if int(since_ms) > asof:
        raise AccountDataError("exchange account time precedes the monitor baseline")
    kinds = ("userFillsByTime", "userFunding", "userNonFundingLedgerUpdates")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        histories = dict(zip(kinds, pool.map(
            lambda kind: fetch_history(kind, wallet, since_ms, asof, post=post), kinds)))
    result = {
        "version": 1, "wallet": wallet, "mode": mode,
        "started_at_ms": started, "received_at_ms": clock(), "asof_ms": asof,
        "history_start_ms": int(since_ms), "venues": venues,
        "fills": histories["userFillsByTime"], "funding": histories["userFunding"],
        "ledger": histories["userNonFundingLedgerUpdates"],
    }
    if spot is not None:
        result["spot"] = spot
    return result
