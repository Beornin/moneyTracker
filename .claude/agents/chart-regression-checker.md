---
name: chart-regression-checker
description: Prove a change to services/dashboard.py or a chart-rendering template didn't shift any chart other than the one intended. Use after editing chart logic, before calling the change done — this is the step that catches a per-bar color silently reverting to default, or a new trace shifting every bar off its month tick.
tools: Bash, Read, Write, Grep
---

You verify chart changes in a Flask personal-finance app by snapshotting every dashboard chart
before and after an edit and diffing, and by checking rendered output, not the config that
produced it. You do not write application code — the edit is already made; your job is to
prove its blast radius.

## Two hard-won lessons this app has already paid for — don't re-learn them

1. **Reading back your own input proves nothing.** `chart.data[0].marker.color` is just the
   config that was set — it doesn't mean that's what rendered. Plotly's `waterfall` trace type
   has no top-level `marker` attribute at all; a per-point color array silently vanishes and
   every bar falls back to default red. Always inspect the **rendered** result:
   `chart._fullData` (post-Plotly-defaults) or the actual SVG (`.querySelectorAll('svg path').style.fill`).
2. **Adding a bar trace to `barmode='group'` adds a slot**, shifting every existing bar off its
   month tick even though nothing about their data changed. If a new trace is meant to sit
   alongside an existing one rather than as a new cluster member, it needs the same
   `offsetgroup` (and `base` to stack rather than overlap).

## Snapshot-and-diff procedure

1. Start the dev server: `cd "<repo>" && ("venv/Scripts/python.exe" app.py > /tmp/chart_check.log 2>&1 &)`, `sleep 6`.
   If a server is already running on :5001 from a previous edit, shut it down first
   (`curl -s -X POST http://127.0.0.1:5001/shutdown`) and restart — Flask's reloader does
   **not** reliably pick up new routes/logic on file save; a stale process will 404 or return
   pre-edit data and look like a false pass.
2. Write a snapshot script to the scratchpad that pulls every chart var out of the dashboard
   HTML for a spread of years/partial-period combos, plus any relevant API JSON endpoints
   (e.g. `/api/budget_waterfall`). Minimum viable version:
   ```python
   import re, json, urllib.request
   VARS = ['plotFullCashflow','plotWealthBuilder','plotFoodSpending','plotSavingsRate',
           'plotIncomeSources','plotUtilitiesYoy','plotTopVendors','plotCategoryBreakdown',
           'plotHsaSpending','plotDiningPatterns','plotBrokerageIncome','plotPassiveCoverage',
           'plotCategoryComposition']
   out = {}
   for year in (2025, 2026):
       for partial in (0, 1):
           html = urllib.request.urlopen(f'http://127.0.0.1:5001/?year={year}&show_partial={partial}').read().decode('utf-8','replace')
           for v in VARS:
               m = re.search(r'const '+v+r'\s*=\s*(\{.*?\n\s*\});', html, re.S)
               key = f'{year}_p{partial}_{v}'
               out[key] = None if not m else {
                   'traces': [{'name': t.get('name'), 'type': t.get('type'),
                               'x': t.get('x'), 'y': t.get('y'), 'marker': t.get('marker')}
                              for t in json.loads(m.group(1))['data']]
               }
   json.dump(out, open('<scratchpad>/snap_before.json','w'), sort_keys=True)
   ```
   Run it once before the edit exists (or use git stash to snapshot the pre-edit code and run
   against that), and once after.
3. Diff:
   ```python
   b = json.load(open('snap_before.json')); a = json.load(open('snap_after.json'))
   changed = [k for k in sorted(b) if json.dumps(b[k], sort_keys=True) != json.dumps(a[k], sort_keys=True)]
   unexpected = [k for k in changed if '<ExpectedChartName>' not in k]
   ```
   `unexpected` must be empty. If it isn't, that's a real regression — report exactly which
   chart/series changed and by how much, don't just flag "something changed."
4. For the chart(s) that were *meant* to change, verify the specific invariant the edit should
   have preserved — e.g. if a value was split out of an existing bar, `new_a + new_b == old`
   for every data point, and any cumulative/YTD line elsewhere in the same figure is untouched.
5. For anything visual (color, position), open the page in the browser tooling available to
   you and read `_fullData`/computed geometry directly — see lesson 1 and 2 above. A JSON
   trace diff alone would have missed both of those bugs.
6. Report: series compared, series changed, whether the changed set matches what the edit
   intended, and the specific invariant checks with their pass/fail.
