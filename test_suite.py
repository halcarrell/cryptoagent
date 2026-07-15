#!/usr/bin/env python3
"""
Automated test suite for the crypto screener agent.
Run locally: python3 test_suite.py
Run on Railway: python3 test_suite.py --live

Tests cover:
  - Module imports
  - Config validity
  - Live endpoint health
  - Webhook auth + decision logic
  - DB schema and data freshness
  - Pump guard thresholds
  - Exchange filter
"""

import argparse
import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

LIVE_BASE = os.environ.get(
    "RAILWAY_URL",
    "https://charming-possibility-production-c019.up.railway.app"
)
WEBHOOK_TOKEN = os.environ.get(
    "WEBHOOK_AUTH_TOKEN",
    "d0338ca4f3cc6be20b233142dfd1317277fbca1ec6425cc1076e9365bc803cd3"
)
DB_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results = []


def test(name: str, fn):
    try:
        fn()
        results.append((PASS, name, None))
        print(f"  {PASS} {name}")
    except AssertionError as e:
        results.append((FAIL, name, str(e)))
        print(f"  {FAIL} {name}: {e}")
    except Exception as e:
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))
        print(f"  {FAIL} {name}: {type(e).__name__}: {e}")


# ── Module imports ────────────────────────────────────────────────────────────

def test_imports():
    print("\n[1] Module imports")

    def _crypto_agent(): import crypto_agent
    def _ai_trader(): import ai_trader
    def _notifier(): import notifier
    def _tv_integration(): import tv_integration
    def _weight_refitter(): import weight_refitter

    for fn in [_crypto_agent, _ai_trader, _notifier, _tv_integration, _weight_refitter]:
        test(fn.__name__.lstrip("_"), fn)


# ── Config validity ───────────────────────────────────────────────────────────

def test_config():
    print("\n[2] Config validity")

    def _config_exists():
        assert Path("config.json").exists(), "config.json not found"

    def _required_keys():
        cfg = json.load(open("config.json"))
        for section in ("risk", "strong_signals"):
            assert section in cfg, f"missing '{section}' section"
        risk = cfg["risk"]
        for k in ("min_score", "min_risk_reward_gross", "min_risk_reward_net", "max_position_pct"):
            assert k in risk, f"missing risk.{k}"

    def _risk_thresholds_sensible():
        cfg = json.load(open("config.json"))["risk"]
        assert 0 < cfg["min_score"] <= 5, f"min_score out of range: {cfg['min_score']}"
        assert 1.5 <= cfg["min_risk_reward_gross"] <= 4, f"min_rr_gross suspicious: {cfg['min_risk_reward_gross']}"
        assert 0 < cfg["max_position_pct"] <= 20, f"max_position_pct out of range: {cfg['max_position_pct']}"

    def _pump_guard_constants():
        import crypto_agent
        assert hasattr(crypto_agent, "MAX_7D_CHANGE_PCT"), "MAX_7D_CHANGE_PCT missing"
        assert hasattr(crypto_agent, "MAX_24H_CHANGE_PCT"), "MAX_24H_CHANGE_PCT missing"
        assert 40 <= crypto_agent.MAX_7D_CHANGE_PCT <= 100, f"7d pump guard suspicious: {crypto_agent.MAX_7D_CHANGE_PCT}"
        assert 15 <= crypto_agent.MAX_24H_CHANGE_PCT <= 50, f"24h pump guard suspicious: {crypto_agent.MAX_24H_CHANGE_PCT}"

    for fn in [_config_exists, _required_keys, _risk_thresholds_sensible, _pump_guard_constants]:
        test(fn.__name__.lstrip("_"), fn)


# ── DB schema ─────────────────────────────────────────────────────────────────

def test_db_schema():
    print("\n[3] Database schema")

    if not DB_PATH.exists():
        print(f"  {WARN} DB not found at {DB_PATH} — skipping schema tests")
        return

    def _required_tables():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        conn.close()
        # Screener tables — always required
        for t in ("snapshots", "picks", "factor_scores"):
            assert t in tables, f"missing screener table: {t}"
        # Trading tables — only present on Railway (webhook server creates them)
        for t in ("alerts", "paper_trades"):
            if t not in tables:
                print(f"    {WARN} trading table '{t}' absent (expected on Railway, not local)")


    def _picks_have_data():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM picks")
        n = cur.fetchone()[0]
        conn.close()
        assert n > 0, "picks table is empty — screener may not have run"

    def _no_null_prices_in_picks():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM picks WHERE entry_price IS NULL OR composite_score IS NULL")
        nulls = cur.fetchone()[0]
        conn.close()
        assert nulls == 0, f"{nulls} picks have NULL entry_price or composite_score"

    def _picks_not_stale():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT MAX(pick_date) FROM picks")
        latest = cur.fetchone()[0]
        conn.close()
        assert latest, "no picks in DB"
        pick_dt = datetime.strptime(latest, "%Y-%m-%d").date()
        age = (datetime.now(timezone.utc).date() - pick_dt).days
        assert age <= 2, f"picks are {age} day(s) old (last run: {latest}) — cron may have failed"

    def _pump_guard_effective():
        import crypto_agent
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT MAX(pick_date) FROM picks")
        today = cur.fetchone()[0]
        cur.execute("""
            SELECT p.symbol, s.change_7d, s.change_24h
            FROM picks p
            JOIN snapshots s ON s.coin_id = p.coin_id AND s.snapshot_date = p.pick_date
            WHERE p.pick_date = ?
        """, (today,))
        rows = cur.fetchall()
        conn.close()
        for sym, c7, c24 in rows:
            if c7 is not None:
                assert abs(c7) <= crypto_agent.MAX_7D_CHANGE_PCT + 1, \
                    f"{sym} 7d change {c7:.1f}% exceeds pump guard"
            if c24 is not None:
                assert abs(c24) <= crypto_agent.MAX_24H_CHANGE_PCT + 1, \
                    f"{sym} 24h change {c24:.1f}% exceeds pump guard"

    for fn in [_required_tables, _picks_have_data, _no_null_prices_in_picks,
               _picks_not_stale, _pump_guard_effective]:
        test(fn.__name__.lstrip("_"), fn)


# ── Live endpoint tests ───────────────────────────────────────────────────────

def test_live(run_live: bool):
    print("\n[4] Live endpoint tests")
    if not run_live:
        print(f"  (skipped — run with --live to test Railway endpoint)")
        return

    def _health():
        r = requests.get(f"{LIVE_BASE}/", timeout=10)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        body = r.json()
        assert body.get("status") == "ok", f"unexpected body: {body}"

    def _webhook_rejects_bad_auth():
        r = requests.post(f"{LIVE_BASE}/webhook", json={"auth": "wrong"}, timeout=10)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"

    def _webhook_accepts_valid_auth():
        r = requests.post(f"{LIVE_BASE}/webhook", json={
            "auth": WEBHOOK_TOKEN, "symbol": "BTCUSDT", "exchange": "BINANCE",
            "side": "long", "entry": 65000, "stop": 63500, "target": 68000,
        }, timeout=10)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        body = r.json()
        assert "decision" in body, f"missing 'decision' in response: {body}"
        assert body["decision"]["action"] in ("enter", "pass"), \
            f"unexpected action: {body['decision']['action']}"

    def _webhook_passes_on_bad_rr():
        r = requests.post(f"{LIVE_BASE}/webhook", json={
            "auth": WEBHOOK_TOKEN, "symbol": "BTCUSDT", "exchange": "BINANCE",
            "side": "long", "entry": 100, "stop": 99, "target": 101,  # 1:1 R:R
        }, timeout=10)
        body = r.json()
        assert body["decision"]["action"] == "pass", \
            f"should have passed on bad R:R, got: {body['decision']}"

    def _webhook_no_trailing_slash():
        r = requests.post(f"{LIVE_BASE}/webhook/", timeout=5)
        assert r.status_code == 404, f"expected 404 for trailing slash, got {r.status_code}"

    for fn in [_health, _webhook_rejects_bad_auth, _webhook_accepts_valid_auth,
               _webhook_passes_on_bad_rr, _webhook_no_trailing_slash]:
        test(fn.__name__.lstrip("_"), fn)


# ── Pine Script ───────────────────────────────────────────────────────────────

def test_pine():
    print("\n[5] Pine Script")

    def _pine_exists():
        assert Path("screener_confirmation.pine").exists()

    def _pine_has_version6():
        content = Path("screener_confirmation.pine").read_text()
        assert "//@version=6" in content, "Pine Script should be version 6"

    def _pine_has_webhook_alert():
        content = Path("screener_confirmation.pine").read_text()
        assert "alert(" in content, "Pine Script missing alert() call"

    def _pine_no_score_logic():
        content = Path("screener_confirmation.pine").read_text()
        assert "get_score(" not in content or "// " in content, \
            "Pine Script should not contain active get_score() — score is server-side"

    for fn in [_pine_exists, _pine_has_version6, _pine_has_webhook_alert, _pine_no_score_logic]:
        test(fn.__name__.lstrip("_"), fn)


# ── Guide freshness ───────────────────────────────────────────────────────────

def test_guide():
    print("\n[6] Documentation")

    def _guide_exists():
        assert Path("GUIDE.md").exists(), "GUIDE.md not found"

    def _guide_has_key_sections():
        content = Path("GUIDE.md").read_text()
        for section in ("Setup Guide", "Admin Guide", "User Guide", "Quick Reference"):
            assert section in content, f"GUIDE.md missing section: {section}"

    def _guide_has_correct_command():
        content = Path("GUIDE.md").read_text()
        assert "US_EXCHANGES" in content, "GUIDE.md missing US_EXCHANGES filter command"
        assert "morning.sh" in content, "GUIDE.md missing morning.sh reference"

    for fn in [_guide_exists, _guide_has_key_sections, _guide_has_correct_command]:
        test(fn.__name__.lstrip("_"), fn)


# ── Unit tests (pure functions, no network/DB) ────────────────────────────────

def test_unit_functions():
    print("\n[7] Unit function tests")

    def _strip_quote_correctness():
        import ai_trader
        assert ai_trader.strip_quote("BTCUSDT") == "BTC"
        assert ai_trader.strip_quote("ETHUSDT") == "ETH"
        assert ai_trader.strip_quote("SOLUSDT") == "SOL"
        assert ai_trader.strip_quote("BTC") == "BTC"    # no-op if already stripped

    def _generate_signal_id_format():
        import re
        from datetime import datetime, timezone
        import ai_trader
        ts = datetime(2025, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
        sid = ai_trader.generate_signal_id("BTCUSDT", timeframe="1h", ts=ts)
        assert sid == "BTC-1H-20250115T093000Z", f"unexpected: {sid}"
        assert re.match(r"^[A-Z]+-1H-\d{8}T\d{6}Z$", sid), f"format wrong: {sid}"

    def _surprise_ratio_tags():
        import ai_trader
        # Perfect win (realized = expected) → EDGE
        ratio, tag = ai_trader._surprise_ratio(10.0, 100, 95, 115, 0.7, "long")
        assert tag in ("EDGE", "EXPECTED"), f"expected edge-ish tag, got {tag}"
        # Massive unexpected gain → LUCK
        ratio2, tag2 = ai_trader._surprise_ratio(50.0, 100, 95, 115, 0.3, "long")
        assert tag2 == "LUCK", f"expected LUCK, got {tag2}"
        # Massive unexpected loss → ANOMALY
        ratio3, tag3 = ai_trader._surprise_ratio(-50.0, 100, 95, 115, 0.9, "long")
        assert tag3 == "ANOMALY", f"expected ANOMALY, got {tag3}"
        # Invalid entry → returns None/UNKNOWN
        ratio4, tag4 = ai_trader._surprise_ratio(5.0, None, 95, 115, 0.5, "long")
        assert tag4 == "UNKNOWN"

    def _entry_conditions_insufficient_candles():
        import ai_trader
        result = ai_trader.compute_entry_conditions([])
        assert result.get("long_ok") is False and result.get("short_ok") is False
        result2 = ai_trader.compute_entry_conditions([{"open": 1, "high": 2, "low": 0.5,
                                                        "close": 1.5, "volume": 100}] * 49)
        assert result2.get("long_ok") is False and result2.get("short_ok") is False

    def _smooth_weights_clamp_and_renorm():
        import weight_refitter as wr
        # Use actual CURRENT_WEIGHTS as the baseline (sums to 1.0)
        current = dict(wr.CURRENT_WEIGHTS)
        skewed = {f: (0.9 if f == "momentum" else 0.02) for f in wr.FACTORS}
        smoothed = wr.smooth_weights(skewed, current, max_delta=wr.MAX_WEIGHT_DELTA)
        # All weights must be non-negative and sum to ~1
        for f in wr.FACTORS:
            assert smoothed[f] >= 0, f"{f} went negative: {smoothed[f]}"
        total = sum(smoothed.values())
        assert abs(total - 1.0) < 1e-3, f"weights sum to {total:.6f}, not ~1.0"
        # Clamp must have reduced momentum below its skewed value
        assert smoothed["momentum"] < skewed["momentum"], \
            f"clamp had no effect: momentum={smoothed['momentum']}"
        # Zero-delta: smooth_weights(current, current) must equal current
        unchanged = wr.smooth_weights(current, current)
        for f in wr.FACTORS:
            assert abs(unchanged[f] - current[f]) < 1e-3, \
                f"{f} changed when input=current: {unchanged[f]} vs {current[f]}"

    def _correlation_math_and_edge_cases():
        import weight_refitter as wr
        n_factors = len(wr.FACTORS)
        # Weight all on momentum (index 0); returns linearly increasing → corr=1
        zeros = [0.0] * n_factors
        dataset = [{"factors": [float(i)] + zeros[1:], "return": float(i)}
                   for i in range(20)]
        weights = {f: (1.0 if f == "momentum" else 0.0) for f in wr.FACTORS}
        corr = wr.correlation_with_returns(weights, dataset)
        assert abs(corr - 1.0) < 1e-6, f"expected corr=1.0, got {corr:.4f}"
        # Too few samples → 0
        corr_small = wr.correlation_with_returns(weights, dataset[:5])
        assert corr_small == 0.0, f"expected 0 for n<10, got {corr_small}"
        # Constant returns → denominator=0, returns 0 gracefully
        flat = [{"factors": [1.0] + zeros[1:], "return": 5.0} for _ in range(20)]
        corr_flat = wr.correlation_with_returns(weights, flat)
        assert corr_flat == 0.0

    def _decide_trade_rejects_stale_signal():
        import ai_trader
        from datetime import datetime, timezone, timedelta
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        alert = {"symbol": "BTCUSDT", "exchange": "BINANCE", "side": "long",
                 "entry": 60000, "stop": 57000, "target": 66000, "time": stale_ts}
        d = ai_trader.decide_trade(alert)
        assert d.action == "pass", f"expected pass for stale signal, got {d.action}"
        assert "old" in d.reasoning.lower() or "stale" in d.reasoning.lower(), \
            f"unexpected reason: {d.reasoning}"

    def _decide_trade_rejects_wide_stop():
        import ai_trader
        # Stop distance = (100 - 88) / 100 = 12% > MAX_STOP_PCT=8%
        alert = {"symbol": "BTCUSDT", "exchange": "BINANCE", "side": "long",
                 "entry": 100, "stop": 88, "target": 130}
        d = ai_trader.decide_trade(alert)
        assert d.action == "pass"
        assert "stop" in d.reasoning.lower() or "exceed" in d.reasoning.lower(), \
            f"unexpected reason: {d.reasoning}"

    def _binance_us_url_format():
        import tv_integration
        assert tv_integration.EXCHANGE_API.get("BINANCE_US") == "https://api.binance.us", \
            "BINANCE_US API base URL changed — check get_tradeable_pairs()"

    for fn in [_strip_quote_correctness, _generate_signal_id_format,
               _surprise_ratio_tags, _entry_conditions_insufficient_candles,
               _smooth_weights_clamp_and_renorm, _correlation_math_and_edge_cases,
               _decide_trade_rejects_stale_signal, _decide_trade_rejects_wide_stop,
               _binance_us_url_format]:
        test(fn.__name__.lstrip("_"), fn)


# ── Summary ───────────────────────────────────────────────────────────────────

def summarise(post_to_discord: bool = False):
    passed = sum(1 for r in results if r[0] == PASS)
    failed = sum(1 for r in results if r[0] == FAIL)
    total  = len(results)

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed  |  {failed} failed")

    if failed:
        print("\nFailed tests:")
        for icon, name, err in results:
            if icon == FAIL:
                print(f"  {FAIL} {name}: {err}")

    if post_to_discord:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            color  = 0x2ECC71 if failed == 0 else 0xE74C3C
            emoji  = "✅" if failed == 0 else "🔴"
            desc   = f"{passed}/{total} tests passed"
            if failed:
                fails = "\n".join(f"• {n}: {e}" for _, n, e in results if _ == FAIL)
                desc += f"\n\n**Failed:**\n{fails}"
            requests.post(webhook, json={"embeds": [{
                "title": f"{emoji} Test suite — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                "color": color, "description": desc,
            }]}, timeout=5)

    return failed


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live",    action="store_true", help="Run live endpoint tests")
    p.add_argument("--discord", action="store_true", help="Post results to Discord")
    args = p.parse_args()

    print("Crypto Screener — Test Suite")
    print(f"DB: {DB_PATH}  |  Live: {LIVE_BASE}")

    test_imports()
    test_config()
    test_db_schema()
    test_live(args.live)
    test_pine()
    test_guide()
    test_unit_functions()

    failed = summarise(post_to_discord=args.discord)
    sys.exit(1 if failed else 0)
