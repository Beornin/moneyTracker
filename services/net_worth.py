from sqlalchemy.orm import joinedload
import plotly.graph_objects as go
from plotly.io import to_json

from models import NetWorthAccount


class NetWorthService:
    """Aggregation + chart logic for the standalone net-worth tracker.
    Reads only NetWorthAccount/NetWorthSnapshot — never Transaction/Account."""

    def __init__(self):
        self.accounts = NetWorthAccount.query.options(
            joinedload(NetWorthAccount.snapshots)
        ).filter_by(is_archived=False).all()
        self.base_layout = dict(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                                 font=dict(color='#71717a'), autosize=True)

    def current_balance(self, account):
        """Most recent snapshot balance, or None if the account has never been snapshotted."""
        if not account.snapshots:
            return None
        return float(max(account.snapshots, key=lambda s: (s.date, s.id)).balance)

    def get_summary(self):
        assets = [a for a in self.accounts if a.item_type == 'asset']
        debts = [a for a in self.accounts if a.item_type == 'debt']

        def countable_total(accts):
            return sum((self.current_balance(a) or 0.0) for a in accts if a.include_in_net_worth)

        total_assets = countable_total(assets)
        total_debts = countable_total(debts)
        return {
            'total_assets': total_assets,
            'total_debts': total_debts,
            'net_worth': total_assets - total_debts,
            'assets_by_owner': self._group(assets),
            'debts_by_owner': self._group(debts),
        }

    def _group(self, accounts):
        grouped = {}
        for a in sorted(accounts, key=lambda a: (a.owner, a.name)):
            grouped.setdefault(a.owner, []).append((a, self.current_balance(a)))
        return grouped

    def get_net_worth_chart(self):
        """Forward-fill step series across every distinct snapshot date."""
        countable = [a for a in self.accounts if a.include_in_net_worth]
        all_dates = sorted({s.date for a in countable for s in a.snapshots})
        if not all_dates:
            return None

        def series_for(account):
            snaps = sorted(account.snapshots, key=lambda s: s.date)
            out, idx, last = [], 0, None
            for d in all_dates:
                while idx < len(snaps) and snaps[idx].date <= d:
                    last = float(snaps[idx].balance)
                    idx += 1
                out.append(last)
            return out

        asset_totals = [0.0] * len(all_dates)
        debt_totals = [0.0] * len(all_dates)
        for a in countable:
            target = asset_totals if a.item_type == 'asset' else debt_totals
            for i, v in enumerate(series_for(a)):
                if v is not None:
                    target[i] += v

        net_totals = [a - d for a, d in zip(asset_totals, debt_totals)]
        x_dates = [d.strftime('%Y-%m-%d') for d in all_dates]

        fig = go.Figure()
        fig.add_trace(go.Scatter(name='Assets', x=x_dates, y=asset_totals,
                                  mode='lines+markers', line=dict(shape='hv', color='#10b981')))
        fig.add_trace(go.Scatter(name='Debts', x=x_dates, y=debt_totals,
                                  mode='lines+markers', line=dict(shape='hv', color='#ef4444')))
        fig.add_trace(go.Scatter(name='Net Worth', x=x_dates, y=net_totals,
                                  mode='lines+markers', line=dict(shape='hv', color='#6366f1', width=3)))
        fig.update_layout(
            title='Net Worth Over Time',
            yaxis=dict(title='$', tickformat="$,.0f"),
            xaxis=dict(type='date', tickformat='%b %d, %Y'),
            showlegend=True,
            legend=dict(orientation='h', yanchor='top', y=-0.2, xanchor='center', x=0.5),
            margin=dict(t=40, b=80, l=50, r=10),
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    # ── Breakdown charts ────────────────────────────────────────────────

    HORIZON_ORDER = ['now', 'now_mid', 'mid_long', 'long']
    HORIZON_LABELS = {'now': 'Now', 'now_mid': 'Now-Mid', 'mid_long': 'Mid-Long', 'long': 'Long', 'unclassified': 'Unclassified'}
    HORIZON_COLORS = {'now': '#ef4444', 'now_mid': '#f59e0b', 'mid_long': '#3b82f6', 'long': '#10b981', 'unclassified': '#9ca3af'}

    def get_horizon_breakdown(self):
        """Net contribution (assets minus debts) to net worth per liquidity-horizon bucket.
        Accounts with no horizon set are grouped under 'unclassified'."""
        totals = {k: 0.0 for k in self.HORIZON_ORDER}
        unclassified = 0.0
        for a in self.accounts:
            if not a.include_in_net_worth:
                continue
            bal = self.current_balance(a) or 0.0
            signed = bal if a.item_type == 'asset' else -bal
            if a.liquidity_horizon in totals:
                totals[a.liquidity_horizon] += signed
            else:
                unclassified += signed

        rows = [{'key': k, 'label': self.HORIZON_LABELS[k], 'value': totals[k]} for k in self.HORIZON_ORDER]
        if unclassified:
            rows.append({'key': 'unclassified', 'label': self.HORIZON_LABELS['unclassified'], 'value': unclassified})
        return rows

    def get_horizon_chart(self):
        rows = [r for r in self.get_horizon_breakdown() if r['value'] != 0]
        if not rows:
            return None

        labels = [r['label'] for r in rows]
        values = [r['value'] for r in rows]
        colors = [self.HORIZON_COLORS.get(r['key'], '#9ca3af') for r in rows]

        fig = go.Figure(go.Bar(
            x=labels, y=values, marker_color=colors,
            text=[f"${v:,.0f}" for v in values], textposition='auto',
            hovertemplate='<b>%{x}</b><br>$%{y:,.2f}<extra></extra>'
        ))
        fig.update_layout(
            title='Net Worth by Liquidity Horizon',
            yaxis=dict(title='$', tickformat="$,.0f", zeroline=True, zerolinecolor='#9ca3af'),
            margin=dict(t=40, b=40, l=50, r=10),
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def get_category_breakdown(self):
        """Asset composition by category, as a % of total countable assets.
        (Debts aren't included — a mortgage isn't a 'category' net worth comes from.)"""
        totals = {}
        for a in self.accounts:
            if a.item_type != 'asset' or not a.include_in_net_worth:
                continue
            bal = self.current_balance(a) or 0.0
            if bal <= 0:
                continue
            totals[a.category] = totals.get(a.category, 0.0) + bal

        total = sum(totals.values())
        if total <= 0:
            return []

        rows = [{'label': k, 'value': v, 'pct': v / total * 100} for k, v in totals.items()]
        rows.sort(key=lambda r: r['value'], reverse=True)
        return rows

    def get_category_chart(self):
        rows = self.get_category_breakdown()
        if not rows:
            return None

        labels = [r['label'] for r in rows]
        values = [r['value'] for r in rows]

        fig = go.Figure(go.Pie(
            labels=labels, values=values, hole=0.45,
            textinfo='percent',
            textposition='inside',
            insidetextorientation='radial',
            hovertemplate='<b>%{label}</b><br>$%{value:,.2f} (%{percent})<extra></extra>'
        ))
        fig.update_layout(
            title='Net Worth Composition by Category',
            showlegend=True,
            legend=dict(orientation='h', yanchor='top', y=-0.15, xanchor='center', x=0.5),
            margin=dict(t=40, b=90, l=20, r=20),
            **self.base_layout
        )
        return to_json(fig, pretty=True)
