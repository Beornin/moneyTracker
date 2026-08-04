# moneyTracker

Single-user Flask + Postgres personal finance app, packaged as a Windows exe. It tracks a
**real household's actual money** — treat the data as production.

## Read this first

**`FINANCIAL_MODEL.md`** (repo root) documents how this household's money actually moves and
why the code makes the choices it does. Read it before touching `services/dashboard.py`,
`services/retirement.py`, or the budget page. Most of the exclusion logic looks arbitrary
without it, and several "obvious fixes" have already been tried and rejected for reasons
recorded there.

**It's gitignored on purpose** — it has real dollar figures and household financial structure,
and this repo is public. The file lives locally on this machine; if it's missing (e.g. a fresh
clone), ask the user for it rather than reconstructing the model from guesses.

## Layout

| Path | Purpose |
|---|---|
| `app.py` | Flask app + most routes (budget routes live here, **not** in `routes/`) |
| `services/dashboard.py` | `DashboardService` — all dashboard charts + the budget waterfall |
| `services/retirement.py` | Monte-Carlo retirement projection + expense pre-fill |
| `services/net_worth.py` | Net worth tracker — **deliberately standalone**, no FK or read path into `Transaction`/`Account` |
| `routes/` | Only `net_worth.py` and `retirement.py` blueprints |
| `constants.py` | Category/entity exclusion sets — **single source of truth, import don't re-declare** |
| `utils/pdf_parsers.py` | Chase / Wells Fargo / HSA PDF + Fidelity CSV parsers |

## Environment

- Real venv is **`venv/`**. (`.venv/` is broken — points at a defunct WindowsApps stub.)
- Run: `venv/Scripts/python.exe app.py` → serves on **port 5001**.
- Postgres: `postgresql://budget_user:ben@localhost:5432/budget_db`
- `app.py` has a **UTF-8 BOM** — parse it with `encoding='utf-8-sig'`, not `'utf-8'`.
- Graceful shutdown: `curl -s -X POST http://127.0.0.1:5001/shutdown`
- Flask's reloader means an already-running server **won't** pick up new routes — restart it
  before concluding a new endpoint 404s.

## Working conventions

- **Rebuild the exe after code changes**, then smoke-test the packaged build:
  ```bash
  venv/Scripts/python.exe -m PyInstaller money_tracker.spec --noconfirm
  ```
- **Verify against the live database**, not by reasoning about the code. Every financial claim
  in this repo's history that turned out wrong was wrong because it wasn't checked against real
  rows.
- **Verify rendered output, not your own input.** Reading back what you just set into a chart
  config proves nothing — check `_fullData` or the rendered SVG.
- **Prove no regressions on chart changes** by snapshotting every chart's series before and
  after and diffing. Charts share helpers (`_net_by_entity`, `_prior_years_surplus`), so edits
  reach further than they look.
- **Schema changes need manual `ALTER TABLE`** — `db.create_all()` only creates missing tables.
- Ask before recategorizing or deleting transaction rows. This is real financial data.
