import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import time

import pytest

import market_brief as mb

FIX = Path(__file__).parent / "fixtures" / "market_brief"
SGT = ZoneInfo("Asia/Singapore")


def text(name):
    return (FIX / name).read_text()


def js(name):
    return json.loads(text(name))


class Resp:
    def __init__(self, body=None, raw=None):
        self._body, self.text = body, raw

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def fake_get(url, **kwargs):
    if "stablecoins" in url:
        return Resp(js("stablecoins.json"))
    if "fredgraph" in url:
        series = url.split("id=")[1]
        return Resp(raw=text(f"fred_{series}.csv"))
    if "yahoo" in url:
        name = {"%5EVIX": "vix", "^VIX": "vix", "DX-Y.NYB": "dxy", "^TNX": "tnx", "%5ETNX": "tnx"}
        symbol = url.split("/chart/")[1].split("?")[0]
        return Resp(js(f"yahoo_{name[symbol]}.json"))
    if "faireconomy" in url:
        return Resp(js("forexfactory.json"))
    raise AssertionError(url)


def fake_post(url, json=None, **kwargs):
    if "hyperliquid" in url:
        if json["type"] == "metaAndAssetCtxs":
            return Resp(js("meta.json"))
        # The same real books serve every coin: the full-precision one, and
        # one 3-significant-figure aggregation for every nSigFigs request.
        return Resp(js("l2book_btc_sig3.json" if "nSigFigs" in json else "l2book_btc.json"))
    if "sosovalue" in url:
        return Resp(js("etf.json"))
    raise AssertionError(url)


CONFIG = mb.BriefConfig()
# 2026-10-03 08:00 SGT, a Saturday; the fixture calendar's events are earlier that week.
NOW = int(datetime(2026, 10, 3, 8, 0, tzinfo=SGT).timestamp() * 1000)


def history(tmp_path, samples=None):
    h = mb.OiHistory(tmp_path / "oi.json")
    for ts, oi in samples or []:
        h.add(ts, oi)
    return h


def full_data():
    return mb.collect(CONFIG, get=fake_get, post=fake_post)


# --- parsing ---------------------------------------------------------------

def test_hl_markets_parse():
    out = mb.fetch_hl_markets(["BTC", "HYPE"], post=fake_post)
    assert out["BTC"]["funding"] == pytest.approx(0.0000125)
    assert out["BTC"]["oi"] == pytest.approx(37991.03272)
    assert out["HYPE"]["mark"] > 0


def test_hl_unknown_coin_raises():
    with pytest.raises(ValueError):
        mb.fetch_hl_markets(["NOPE"], post=fake_post)


def test_book_depth_on_a_fixed_book():
    book = {"levels": [
        [{"px": "100", "sz": "1"}, {"px": "99.5", "sz": "2"}, {"px": "98", "sz": "5"}],
        [{"px": "100.2", "sz": "1"}, {"px": "100.8", "sz": "3"}, {"px": "102", "sz": "9"}],
    ]}
    out = mb.book_depth(book)
    mid = 100.1
    assert out["spread_bp"] == pytest.approx(0.2 / mid * 1e4)
    # bids >= 99.099 and asks <= 101.101: 100*1 + 99.5*2 + 100.2*1 + 100.8*3
    assert out["depth_usd"] == pytest.approx(100 + 199 + 100.2 + 302.4)
    assert out["truncated"] is False


def test_book_depth_flags_a_book_too_shallow_to_reach_the_band():
    book = {"levels": [[{"px": "100", "sz": "1"}, {"px": "99.9", "sz": "1"}],
                       [{"px": "100.1", "sz": "1"}, {"px": "100.2", "sz": "1"}]]}
    assert mb.book_depth(book)["truncated"] is True


def test_real_btc_book_fixture_parses():
    out = mb.book_depth(js("l2book_btc.json"))
    assert out["depth_usd"] > 0 and out["spread_bp"] >= 0


def _book(bid_px, ask_px, size="1"):
    return {"levels": [[{"px": str(p), "sz": size} for p in bid_px],
                       [{"px": str(p), "sz": size} for p in ask_px]]}


def test_depth_uses_the_finest_aggregation_that_reaches_the_band():
    full = _book([100, 99.99], [100.01, 100.02])                 # spread 1bp, reaches nowhere near 1%
    sig4 = _book([100, 99.9], [100.1, 100.2])                    # still short of 99.0 / 101.0
    sig3 = _book([100, 99.5, 98], [100.5, 101, 102], size="2")   # reaches past both edges
    calls = []

    def post(url, json=None, **kwargs):
        calls.append(json.get("nSigFigs"))
        return Resp({None: full, 4: sig4, 3: sig3}[json.get("nSigFigs")])

    out = mb.fetch_hl_depth("BTC", post=post)
    assert calls == [None, 4, 3]                                 # stops at the first book that reaches
    assert out["truncated"] is False
    assert out["spread_bp"] == pytest.approx(0.01 / 100.005 * 1e4)  # from the full book, not the buckets
    # band around the full-book mid 100.005: bids >= 99.00495, asks <= 101.00505
    assert out["depth_usd"] == pytest.approx(2 * (100 + 99.5) + 2 * (100.5 + 101))


def test_depth_is_a_flagged_lower_bound_when_no_aggregation_reaches():
    shallow = _book([100, 99.9], [100.1, 100.2])

    def post(url, json=None, **kwargs):
        return Resp(shallow)

    out = mb.fetch_hl_depth("BTC", post=post)
    assert out["truncated"] is True


def test_real_btc_books_reach_the_band():
    out = mb.fetch_hl_depth("BTC", post=fake_post)
    assert out["truncated"] is False and out["depth_usd"] > 0


def test_stablecoins_parse():
    out = mb.fetch_stablecoins(get=fake_get)
    assert out["total_usd"] == pytest.approx(183743409500.28595 + 74140791080.4518)
    assert out["week_change_usd"] == pytest.approx(
        (183743409500.28595 - 183716716061.87643) + (74140791080.4518 - 76436728300.73679))


def test_net_liquidity_unit_conversion_rrp_is_billions():
    # WALCL/WTREGEN in millions, RRP in billions.
    walcl = [("2026-09-23", 6_000_000.0), ("2026-09-30", 6_100_000.0)]
    tga = [("2026-09-23", 800_000.0), ("2026-09-30", 900_000.0)]
    rrp = [("2026-09-23", 10.0), ("2026-09-29", 20.0)]
    out = mb.net_liquidity(walcl, tga, rrp)
    assert out["net_b"] == pytest.approx(6100 - 900 - 20)
    assert out["week_change_b"] == pytest.approx((6100 - 900 - 20) - (6000 - 800 - 10))


def test_fred_csv_skips_missing_observations():
    rows = mb.parse_fred_csv("observation_date,X\n2026-01-01,.\n2026-01-02,5\n")
    assert rows == [("2026-01-02", 5.0)]


def test_net_liquidity_from_fixtures_is_plausible():
    out = mb.fetch_net_liquidity(get=fake_get)
    assert 4000 < out["net_b"] < 9000


def test_yahoo_uses_previous_close_not_range_start():
    out = mb.fetch_yahoo("^VIX", get=fake_get)
    assert out["price"] == pytest.approx(15.67)
    assert out["prev"] == pytest.approx(16.39, abs=0.01)


def test_etf_takes_latest_date():
    out = mb.fetch_etf_flow(post=fake_post)
    assert out["date"] == "2026-10-01"
    assert out["net_inflow_usd"] == pytest.approx(102670557.075)


def test_events_are_high_impact_usd_only():
    events = mb.fetch_events(get=fake_get)
    assert events and all(isinstance(s, datetime) for s, _ in events)
    assert "Non-Farm Employment Change" in [t for _, t in events]
    assert events == sorted(events)


# --- message ---------------------------------------------------------------

def test_full_briefing_renders_every_section_under_the_cap(tmp_path):
    data = full_data()
    assert data["unavailable"] == []
    out = mb.format_brief(data, CONFIG, history(tmp_path), NOW)
    assert out.startswith("Liquidity briefing — Sat 3 Oct, 08:00 SGT")
    for part in ("MACRO", "Fed net liquidity", "VIX 15.7", "CRYPTO MONEY", "Stablecoins",
                 "BTC ETF flow (2026-10-01)  +$102.7M", "HYPERLIQUID", "BTC", "warming up",
                 "Unavailable: none", "Conditions, not signals."):
        assert part in out
    assert len(out) < mb.MAX_TEXT


def test_one_source_failing_does_not_stop_the_others(tmp_path):
    def broken_get(url, **kw):
        if "yahoo" in url:
            raise RuntimeError("boom")
        return fake_get(url, **kw)

    def junk_post(url, json=None, **kw):
        if "sosovalue" in url:
            return Resp({"unexpected": 1})
        return fake_post(url, json=json, **kw)

    data = mb.collect(CONFIG, get=broken_get, post=junk_post)
    for name in ("VIX (Yahoo)", "Dollar index (Yahoo)", "US 10y (Yahoo)", "BTC ETF flow (SoSoValue)"):
        assert name in data["unavailable"]
    out = mb.format_brief(data, CONFIG, history(tmp_path), NOW)
    assert "Unavailable: " in out and "VIX (Yahoo)" in out and "BTC ETF flow (SoSoValue)" in out
    assert "Fed net liquidity" in out and "Stablecoins" in out and "HYPERLIQUID" in out
    assert "VIX 1" not in out


def test_everything_down_still_renders_a_message(tmp_path):
    def down(*a, **k):
        raise RuntimeError("no network")
    data = mb.collect(CONFIG, get=down, post=down)
    out = mb.format_brief(data, CONFIG, history(tmp_path), NOW)
    assert out.count(",") >= 8 and "Conditions, not signals." in out


def test_late_marker_and_events_in_next_24h(tmp_path):
    data = full_data()
    thursday = int(datetime(2026, 10, 2, 7, 0, tzinfo=SGT).timestamp() * 1000)  # NFP is 20:30 SGT that day
    out = mb.format_brief(data, CONFIG, history(tmp_path), thursday, late=True)
    assert "(late)" in out and "20:30  USD  Non-Farm Employment Change (high)" in out


# --- OI history ------------------------------------------------------------

def test_oi_warming_up_then_change(tmp_path):
    h = history(tmp_path, [(NOW - 3600_000, {"BTC": 100.0})])
    assert h.change_pct("BTC", NOW - 1800_000, 3600) is None  # warming up
    h.add(NOW, {"BTC": 90.0})
    assert h.change_pct("BTC", NOW, 3600) == pytest.approx(-10.0)
    assert h.change_pct("BTC", NOW, 24 * 3600) is None


def test_oi_history_survives_restart_and_prunes(tmp_path):
    history(tmp_path, [(NOW - 30 * 3600_000, {"BTC": 1.0}), (NOW, {"BTC": 2.0})])
    again = mb.OiHistory(tmp_path / "oi.json")
    assert again.change_pct("BTC", NOW, 3600) is None
    assert len(again._samples["BTC"]) == 1  # the 30h-old sample was pruned


def test_oi_gap_does_not_stretch_the_last_hour(tmp_path):
    h = history(tmp_path, [(NOW - 4 * 3600_000, {"BTC": 100.0}), (NOW, {"BTC": 90.0})])
    assert h.change_pct("BTC", NOW, 3600) is None  # the only baseline is 4 hours old


def test_corrupt_oi_file_starts_empty(tmp_path):
    (tmp_path / "oi.json").write_text("{not json")
    assert mb.OiHistory(tmp_path / "oi.json").change_pct("BTC", NOW, 3600) is None


# --- alerts ----------------------------------------------------------------

def alert_data(funding=0.0, events=None):
    return {"results": {"Hyperliquid markets": {"BTC": {"funding": funding, "oi": 90.0, "mark": 1, "volume_24h": 1}},
                        "Economic calendar (ForexFactory)": events or []},
            "unavailable": []}


def test_funding_alert_both_directions_and_threshold_edge(tmp_path):
    cfg = mb.BriefConfig(coins=["BTC"])
    h = history(tmp_path)
    assert mb.evaluate_alerts(alert_data(0.00004), cfg, h, NOW) == []
    for f in (0.00005, -0.00005, 0.0001):
        keys = [k for k, _ in mb.evaluate_alerts(alert_data(f), cfg, h, NOW)]
        assert keys == ["funding:BTC"]


def test_flush_alert_needs_5pct_in_an_hour(tmp_path):
    cfg = mb.BriefConfig(coins=["BTC"])
    h = history(tmp_path, [(NOW - 3600_000, {"BTC": 100.0}), (NOW, {"BTC": 95.0})])
    assert [k for k, _ in mb.evaluate_alerts(alert_data(), cfg, h, NOW)] == ["flush:BTC"]
    h.add(NOW + 1000, {"BTC": 96.0})
    assert mb.evaluate_alerts(alert_data(), cfg, h, NOW + 1000) == []


def test_event_alert_within_60_minutes_only():
    cfg = mb.BriefConfig(coins=["BTC"])
    soon = datetime(2026, 10, 3, 8, 45, tzinfo=SGT)
    far = datetime(2026, 10, 3, 9, 30, tzinfo=SGT)
    past = datetime(2026, 10, 3, 7, 59, tzinfo=SGT)
    out = mb.evaluate_alerts(alert_data(events=[(soon, "CPI"), (far, "FOMC"), (past, "Old")]),
                             cfg, mb.OiHistory(Path("/nonexistent/oi.json")), NOW)
    assert len(out) == 1 and "CPI" in out[0][1] and "in 45 min" in out[0][1]


# --- scheduler -------------------------------------------------------------

class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


def make_scheduler(tmp_path, start_ms, data=None, config=None):
    sent, clock = [], Clock(start_ms)
    calls = []

    def fetch(cfg):
        calls.append(1)
        return data if data is not None else alert_data()
    sched = mb.BriefScheduler(config or mb.BriefConfig(coins=["BTC"]), tmp_path, sent.append,
                              fetch=fetch, clock=clock, sleep=lambda s: None, spawn=lambda fn: fn())
    return sched, sent, clock, calls


def briefs(sent):
    return [m for m in sent if m.startswith("Liquidity briefing")]


def test_slot_fires_once_and_not_before_time(tmp_path):
    before = NOW - 60_000
    sched, sent, clock, _ = make_scheduler(tmp_path, before)
    sched.tick()
    assert briefs(sent) == []
    clock.ms = NOW + 5_000
    sched.tick()
    clock.ms = NOW + 40_000
    sched.tick()
    assert len(briefs(sent)) == 1 and "(late)" not in briefs(sent)[0]


def test_no_double_send_across_a_restart(tmp_path):
    sched, sent, clock, _ = make_scheduler(tmp_path, NOW + 5_000)
    sched.tick()
    assert len(briefs(sent)) == 1
    sched2, sent2, _, _ = make_scheduler(tmp_path, NOW + 120_000)
    sched2.tick()
    assert briefs(sent2) == []


def test_late_send_inside_30_minutes_is_marked_late(tmp_path):
    sched, sent, _, _ = make_scheduler(tmp_path, NOW + 20 * 60_000)
    sched.tick()
    assert len(briefs(sent)) == 1 and "(late)" in briefs(sent)[0]


def test_slot_older_than_30_minutes_is_skipped_and_not_resent(tmp_path):
    sched, sent, clock, _ = make_scheduler(tmp_path, NOW + 31 * 60_000)
    sched.tick()
    clock.ms += 60_000
    sched.tick()
    assert briefs(sent) == []


def test_both_daily_slots_fire(tmp_path):
    sched, sent, clock, _ = make_scheduler(tmp_path, NOW + 1000)
    sched.tick()
    clock.ms = NOW + (12 * 60 + 30) * 60_000 + 1000  # 20:30
    sched.tick()
    assert len(briefs(sent)) == 2


def test_alert_repeat_limit_is_4_hours(tmp_path):
    hot = alert_data(funding=0.0001)
    sched, sent, clock, _ = make_scheduler(tmp_path, NOW - 3600_000 * 2, data=hot)
    sched.tick()

    def alerts():
        return [m for m in sent if m.startswith("LIQUIDITY ALERT")]

    assert len(alerts()) == 1
    clock.ms += 3 * 3600_000 + 60_000
    sched.tick()
    assert len(alerts()) == 1  # inside 4 hours
    clock.ms += 3600_000
    sched.tick()
    assert len(alerts()) == 2


def test_alert_limit_survives_a_restart(tmp_path):
    hot = alert_data(funding=0.0001)
    t0 = NOW - 3600_000 * 2
    sched, sent, _, _ = make_scheduler(tmp_path, t0, data=hot)
    sched.tick()
    sched2, sent2, _, _ = make_scheduler(tmp_path, t0 + 20 * 60_000, data=hot)
    sched2.tick()
    assert sent2 == []


def test_samples_oi_every_15_minutes_only(tmp_path):
    sched, sent, clock, calls = make_scheduler(tmp_path, NOW - 3600_000 * 2)
    sched.tick()
    clock.ms += 10 * 60_000
    sched.tick()
    assert len(calls) == 1
    clock.ms += 5 * 60_000
    sched.tick()
    assert len(calls) == 2


def test_liquidity_reply_reuses_a_briefing_under_5_minutes_old(tmp_path):
    sched, sent, clock, calls = make_scheduler(tmp_path, NOW)
    assert sched.liquidity_reply() == mb.LIQUIDITY_PENDING_TEXT  # built off-thread, then sent
    first = briefs(sent)[-1]
    clock.ms += 4 * 60_000
    assert sched.liquidity_reply() == first and len(calls) == 1
    clock.ms += 2 * 60_000
    sched.liquidity_reply()
    assert len(calls) == 2


def test_liquidity_reply_never_blocks_and_starts_one_build_at_a_time(tmp_path):
    spawned = []
    sent = []
    sched = mb.BriefScheduler(mb.BriefConfig(coins=["BTC"]), tmp_path, sent.append,
                              fetch=lambda cfg: alert_data(), clock=Clock(NOW),
                              sleep=lambda s: None, spawn=spawned.append)
    assert sched.liquidity_reply() == mb.LIQUIDITY_PENDING_TEXT
    assert sched.liquidity_reply() == mb.LIQUIDITY_PENDING_TEXT
    assert len(spawned) == 1 and sent == []  # nothing fetched on the caller's thread
    spawned[0]()
    assert len(briefs(sent)) == 1
    sched.liquidity_reply()
    assert len(spawned) == 1  # the fresh briefing is cached now


def test_liquidity_build_failure_is_reported_and_releases_the_build(tmp_path):
    sent = []
    def fetch(cfg):
        raise RuntimeError("boom")
    sched = mb.BriefScheduler(mb.BriefConfig(coins=["BTC"]), tmp_path, sent.append, fetch=fetch,
                              clock=Clock(NOW), sleep=lambda s: None, spawn=lambda fn: fn())
    sched.liquidity_reply()
    sched.liquidity_reply()
    assert sent == ["Could not build the liquidity briefing — see the watcher logs."] * 2


def test_liquidity_requests_do_not_mute_alerts(tmp_path):
    hot = alert_data(funding=0.0001)
    sched, sent, clock, _ = make_scheduler(tmp_path, NOW - 3600_000 * 2, data=hot)
    for _ in range(4):  # a /liquidity every 10 minutes keeps resetting the sampling timer
        clock.ms += 10 * 60_000 + 1
        sched.liquidity_reply()
        sched.tick()
    assert len([m for m in sent if m.startswith("LIQUIDITY ALERT")]) == 1


def test_two_overdue_slots_are_both_skipped(tmp_path):
    # 22:00 SGT with no state: 08:00 is 14h late and 20:30 is 90 minutes late.
    sched, sent, _, _ = make_scheduler(tmp_path, NOW + 14 * 3600_000)
    sched.tick()
    assert briefs(sent) == []


def test_collect_deadline_marks_a_hung_source_unavailable():
    import threading as _t
    release = _t.Event()

    def slow_get(url, **kwargs):
        if "llama" in url:
            release.wait(5)
        return fake_get(url, **kwargs)
    started = time.monotonic()
    data = mb.collect(mb.BriefConfig(coins=["BTC"]), get=slow_get, post=fake_post, deadline_s=0.5)
    release.set()
    assert time.monotonic() - started < 3
    assert "Stablecoins (DefiLlama)" in data["unavailable"]
    assert data["results"]["Hyperliquid markets"] is not None


def test_tick_failure_in_the_fetch_is_contained_by_run_loop(tmp_path):
    def fetch(cfg):
        raise RuntimeError("boom")
    sent = []
    sched = mb.BriefScheduler(mb.BriefConfig(), tmp_path, sent.append, fetch=fetch,
                              clock=Clock(NOW), sleep=lambda s: (_ for _ in ()).throw(StopIteration))
    with pytest.raises(StopIteration):  # sleep reached = tick's exception was caught
        sched.run_forever()
    assert sent == []


# --- config ----------------------------------------------------------------

def test_config_defaults_and_overrides():
    cfg = mb.BriefConfig.from_env({})
    assert cfg.enabled and cfg.times == ["08:00", "20:30"] and cfg.coins == ["BTC", "ETH", "HYPE"]
    assert (cfg.funding_alert_pct, cfg.oi_drop_pct, cfg.event_minutes) == (0.005, 5.0, 60.0)
    cfg = mb.BriefConfig.from_env({"BRIEF_ENABLED": "false", "BRIEF_TIMES": "9:5,bad", "BRIEF_COINS": "sol, btc",
                                   "BRIEF_OI_DROP_PCT": "x"})
    assert not cfg.enabled and cfg.times == ["09:05"] and cfg.coins == ["SOL", "BTC"] and cfg.oi_drop_pct == 5.0
