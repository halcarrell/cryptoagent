# AGENTS.md

## Cursor Cloud specific instructions

This repo is a Python crypto screener + TradingView webhook + paper-trading agent (no
separate frontend). Python 3.12 and a `venv/` virtualenv are used.

### Services / how to run
- **Webhook server** (`webhook_server.py`): Flask app on port `8080` (override with `PORT`).
  Run: `./venv/bin/python webhook_server.py`. Endpoints require an `auth` field/param equal
  to `WEBHOOK_AUTH_TOKEN` (defaults to the insecure `CHANGE_ME`; export a real token first).
  `/` is an unauthenticated health check; `/status`, `/webhook`, `/picks`, etc. need auth.
  The server auto-creates `crypto_agent.db` (SQLite, git-ignored) on first run.
- **CLI screener** (`crypto_agent.py fetch|evaluate|report`) and `tv_integration.py` reach the
  external Binance.US API (`config.json` -> `api_base`). These may fail in restricted/geo-blocked
  network environments; that is not an environment-setup problem.
- **Production entrypoint** (Procfile/railway.toml): `gunicorn -w 1 --threads 4 webhook_server:app`.
  For dev, prefer `python webhook_server.py`.

### Tests
- `./venv/bin/python test_suite.py` runs the suite offline. Add `--live` to hit the deployed
  Railway endpoint (needs network + `RAILWAY_URL`/`WEBHOOK_AUTH_TOKEN`). Do not hardcode the
  production auth token in the repo — set it via env for `--live` runs.
- Offline unit tests should all pass against committed content. Schema/DB freshness tests skip
  (or fail locally) when `crypto_agent.db` is absent — that is expected in a clean checkout.

### Notes
- No linter is configured in this repo.
- `crypto_agent.db*`, `venv/`, `.env*`, and generated `watchlist_*.txt`/`scores_*.pine` are
  git-ignored — never commit them.
- Shared score-bucket recommendation logic lives in `score_analysis.py` (used by `/analysis`
  and Sunday `auto_tune_config`). Dead zones require weak hit rate **and** weak avg 3d return.
