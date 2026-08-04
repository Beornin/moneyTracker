# Financial Model

How this household's money actually moves, and how the app encodes it. Read this before
changing anything in `services/dashboard.py`, `services/retirement.py`, or the budget page —
most of the logic here looks arbitrary without the context below.

---

## 1. How the money actually flows

**Checking (Wells Fargo) is the only hub.** Everything enters and leaves through it.

```
  Salary, Rental, Reimbursements ──▶ CHECKING ──▶ Chase card (most spending)
                                        │   ▲     └─▶ direct spend (some)
                                        │   │
              Way2Save buffer ◀─────────┤   │
        (overdraft cushion, ~untouched) │   │
                                        ▼   │
             Joint Fidelity  ───────────┴───┤   emergency / "middle bucket"
             Ben Fidelity    ───────────────┤   personal sinking fund
             Tucker Fidelity ───────────────┤   kid's savings/MM
             VUL premiums    ◀──────────────┤   life insurance cash value
             Inherited IRA   ───────────────┘   mandatory drawdown, depleting
```

**Data entry cadence:** Fidelity + checking entered manually on the 1st of each month;
Chase PDF uploaded when the statement arrives.

---

## 2. The three kinds of outflow

Money leaving checking is **not** all "spending". Three distinct kinds:

| Bucket | Examples | Counts against us? | Identified by |
|---|---|---|---|
| **1. Operating expense** | Groceries, mortgage, JEA, Eat Out | **Yes** | everything not below |
| **2. Wealth-building** | VUL premiums, → Joint Fidelity, savings buffer, Florida Prepaid | **No** — increases net worth | **category** (`INTERNAL_FLOW_CATS`) |
| **3. Sinking / personal** | Ben Fidelity, Tucker Fidelity | **No** — deferred consumption | **entity** (`BUCKET_ENTITY_NAMES`) |

Bucket 3 must be identified by *entity*, not category: `Other Expense` holds Ben Fidelity
**and** ATM withdrawals **and** Sunbiz, so the category can't be blanket-excluded.

---

## 3. Account semantics — the non-obvious ones

| Entity | Category | What it really is |
|---|---|---|
| **Joint Fidelity** | `Investment` | Emergency fund / "middle bucket". Draws from here are the meaningful signal that income didn't cover the month. Flagged **red** on the wealth chart. |
| **Ben Fidelity** | `Other Expense` | Ben's *personal* spending/investing account. **Not** family investing. Contributions are genuine household expenses (see Model A below). |
| **Tucker Fidelity** | `Kids Invest` | Tucker's savings/MM account. Despite the name, **not** family investing. |
| **Empower (IRA)** | `Empower IRA` | **Inherited** IRA. Mandatory annual drawdown, account depletes. Tracked in Net Worth as "Inherited IRA After". Distributions are *relocating existing wealth*, not earnings. |
| **Way2Save** | `Investment` | Overdraft cushion. Deliberately **not** a funding bucket. |

### Model A (settled, do not re-litigate)

Money moving into Ben/Tucker Fidelity **is** a household expense the moment it leaves
checking — it's left the family pot. Money coming back is treated symmetrically.

Two earlier readings were considered and rejected:
1. ~~"They're misclassified family investments"~~ — no, they're personal.
2. ~~"Contributions shouldn't count since the purchase is counted too"~~ — the net washes out;
   Model A is what the user wants.

---

## 4. What each chart answers

The charts deliberately disagree. That's not a bug — each answers a different question.

| Chart | Question | Investing = expense? | IRA = income? | Bucket draws |
|---|---|---|---|---|
| **Full Income vs Full Expense** | Do we spend more than we take in, **raw**? | **Yes** (by design) | counted in cumulative; **split into its own bar** | folded into income |
| **In w/ Investments vs Out w/o Investments** | Are we **building wealth**? | No | **split into its own bar** | own red bar (Joint Fidelity) |
| **Where Did It Go** (budget page) | Can we **live on normal income**? | No | **fully excluded** | own cyan "available funds" bars |

### Key invariants
- The wealth chart's Expenses **bar** and its cumulative **line** must use the *same*
  exclusion set. They drifted once and VUL ($12,950/yr) showed as spending on the very chart
  built to exclude investing.
- On both cash-flow charts the IRA is peeled off the income **bar** by subtraction, leaving
  the cumulative lines untouched. Full-vs-Full's cumulative still includes it — that chart is
  the raw view on purpose.

---

## 5. Constants (`constants.py`) — single source of truth

```python
EXCLUDED_CAT            = 'Ignored Credit Card Payment'   # CC payment from checking, not real spend
INTERNAL_FLOW_CATS      = {'Savings Transfer','Investment Transfer','Investment','VUL'}
INVESTMENT_INCOME_CATS  = {'Empower IRA','Investment Income'}
BUCKET_ENTITY_NAMES     = {'Joint Fidelity','Ben Fidelity','Tucker Fidelity'}
JOINT_FIDELITY_ENTITY_NAME = 'Joint Fidelity'
```

⚠️ **`INTERNAL_FLOW_CATS` was hand-copied into five places and one copy drifted**, which is
exactly how VUL leaked onto the wealth chart. Import from `constants.py`. Do not re-declare.
(`_chart_wealth_builder` still has a local copy — consolidating it is safe but unverified.)

⚠️ `Investment` and `VUL` are **not seeded by any code path** — they were created by hand.
Renaming either silently disables internal-flow handling across five charts with no error.

---

## 6. Need vs Want

`Category.priority` — `'need' | 'want' | NULL`. Tagged **per category**, once, via the
"Categorize Categories" modal on `/budget`. Every line item and transaction inherits from its
category (entity-linked items resolve via `entity.category`). Deliberately *not* per-entity —
the user will not hand-tag every merchant inside Groceries.

Added by a manual `ALTER TABLE category ADD COLUMN priority VARCHAR(10)` —
`db.create_all()` only creates missing tables, it does not alter existing ones.

---

## 7. Gotchas that cost real debugging time

- **Plotly `waterfall` has no top-level `marker` attribute.** A per-point `marker.color` array
  is *silently dropped* and every decreasing bar renders in one default red. The budget
  waterfall is therefore a `go.Bar` with a per-point `base` stepping the running total.
- **Adding a bar trace to a `barmode='group'` chart adds a slot** and shifts every bar off its
  tick. Use `offsetgroup` (+ `base` to stack) to keep the original slot count.
- **Verify the *rendered* output, not your input.** Reading back `chart.data[0].marker.color`
  only echoes what you set. Read `_fullData` / the SVG `style.fill`.
- **`Category.type` is nearly decorative** — only `services/retirement.py` reads it. Everything
  else keys off the `amount` sign plus category-name matching. The data already contradicts it
  (`Investment` is typed `Expense` but holds positive rows).
- **`_net_by_entity` freezes `category_name` at first-seen**, so an entity with rows in two
  categories in one period attributes its whole net to whichever came first.
- Entity netting is **per-period**. A January contribution and an August drawdown never cancel.

---

## 8. Known-open items

- `/monthly_averages` returns **500** — `templates/monthly_averages.html` was deleted in commit
  `58b922e`. Pre-existing, unrelated to any recent work.
- **Chase parser residue — 123 rows carry a trailing-dash artifact** in
  `original_description` (`... CA -`), from the old parser leaving pdfplumber's split minus
  sign in the description. The parser is fixed; the data was never cleaned. Investigated
  2026-08-03 — **only one group is an actual defect**:

  | Group | Rows | Verdict |
  |---|---|---|
  | **`Facebk` / `Reimbursements`** | **57 pos + 57 neg = 114** | **Duplicates.** Perfectly balanced per (date, amount); entity nets to exactly **$0.00**. Real defect. |
  | `Amazon Mktpl` (22), `Target` (5), `Instacart` (3), `Diapers`, `Rupa Labs`, ~10 singles | ~64 | **Genuine refunds.** Zero same-day opposite rows — correctly positive, leave alone. |
  | `Payment Thank You` (CC payments) | 20 | Correctly positive; `Ignored Credit Card Payment` is excluded from every calculation anyway. |
  | `Frownies`, `Blake's Fun Bounce` | 2 | Ambiguous — same date/amount opposite pair, but *different* description text, so plausibly a real same-day refund rather than a duplicate. |

  **Impact is small and mostly self-cancelling**: the Facebk pairs are same-date, so per-period
  entity netting already cancels them everywhere netting is used. They only inflate *gross*
  figures — income and expense each by $384.72 — plus 114 phantom rows in the ledger.

  ⚠️ Do **not** bulk-flip every artifact row's sign. Roughly half are legitimate refunds; the
  artifact correlates with Chase's credits section, not with being wrong.
- Budget **annual/quarterly** line items ignore the selected month entirely
  (`app.py:1221-1245`); the variables meant to bound them are computed at `app.py:1180` and
  never used. Currently latent — the active plan has none.
- Budget totals mix normalized and un-normalized actuals (`app.py:1270-1273`), so the summary
  cards can't reconcile to the rows beneath them.
- Budget actuals use `abs()`, so refunds *increase* recorded spend (`app.py:1243, 1267`).
- Retirement model: tax brackets are hardcoded 2025 and **never inflation-indexed** while
  expenses and SS are — structurally overstates future tax. Single RMD divisor (25.6) for all
  ages 75+. Normal (not lognormal) return draws. Brokerage withdrawals taxed as 100% gain.

A fuller audit lives in the plan file at
`~/.claude/plans/stateless-plotting-oasis.md`.
