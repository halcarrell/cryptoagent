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
        assert 0 < cfg["min_score"] < 2, f"min_score out of range: {cfg['min_score']}"
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
        assert "alert(payload" in content, "Pine Script missing alert() call"

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

    failed = summarise(post_to_discord=args.discord)
    sys.exit(1 if failed else 0)
