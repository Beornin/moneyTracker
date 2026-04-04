import calendar
from datetime import date, timedelta
from sqlalchemy import func
from sqlalchemy.orm import joinedload
import plotly.graph_objects as go
from plotly.io import to_json

from constants import EXCLUDED_CAT, DASHBOARD_MONTH_SPAN
from models import (
    db, Account, StatementRecord, Category, Transaction, Event, Entity,
    get_active_budget, get_budget_core_filters, is_transaction_budgeted
)


class DashboardService:
    def __init__(self, view_mode='monthly', year=None):
        self.view_mode = view_mode

        core_types = ['checking', 'credit_card']

        type_max_dates = db.session.query(func.max(StatementRecord.end_date))\
            .join(Account)\
            .filter(Account.account_type.in_(core_types))\
            .group_by(Account.account_type)\
            .all()

        valid_dates = [d[0] for d in type_max_dates if d[0] is not None]

        if valid_dates:
            self.today = min(valid_dates)
        else:
            self.today = date.today()

        self.display_year = year if year else self.today.year

        hsa_max_date = db.session.query(func.max(StatementRecord.end_date))\
            .join(Account)\
            .filter(Account.account_type == 'hsa').scalar()

        self.hsa_today = hsa_max_date if hsa_max_date else date.today()

        months_back = max(24, DASHBOARD_MONTH_SPAN + 6)

        y_hist, m_hist = self.today.year, self.today.month - months_back
        while m_hist <= 0:
            m_hist += 12
            y_hist -= 1

        self.fetch_start_date = date(y_hist, m_hist, 1)

        all_txs = Transaction.query.options(
            joinedload(Transaction.category),
            joinedload(Transaction.account),
            joinedload(Transaction.entity)
        ).filter(
            Transaction.date >= self.fetch_start_date,
            Transaction.is_deleted == False
        ).all()

        self.events = Event.query.filter(Event.date >= self.fetch_start_date).all()
        self.savings_account_ids = {a.id for a in Account.query.filter_by(account_type='savings').all()}
        self.hsa_account_ids = {a.id for a in Account.query.filter_by(account_type='hsa').all()}
        self.brokerage_account_ids = {a.id for a in Account.query.filter_by(account_type='brokerage').all()}

        excluded_ids = self.hsa_account_ids | self.brokerage_account_ids
        self.core_transactions = [t for t in all_txs if t.account_id not in excluded_ids]
        self.hsa_transactions = [t for t in all_txs if t.account_id in self.hsa_account_ids]
        self.brokerage_transactions = [t for t in all_txs if t.account_id in self.brokerage_account_ids]

        if self.brokerage_account_ids:
            self.brokerage_all_txs = Transaction.query.options(
                joinedload(Transaction.entity)
            ).filter(
                Transaction.account_id.in_(self.brokerage_account_ids),
                Transaction.is_deleted == False
            ).all()
        else:
            self.brokerage_all_txs = []

        self.active_budget = get_active_budget()
        self.budget_line_items = get_budget_core_filters(self.active_budget)
        self.has_budget = self.active_budget is not None

        self.base_layout = dict(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        self.margin_std = dict(t=40, b=20, l=50, r=10)
        self.margin_legend = dict(t=40, b=100, l=50, r=10)
        self.margin_events = dict(t=60, b=20, l=50, r=10)

        tick_fmt = '%b %Y' if self.view_mode == 'monthly' else '%b %d'
        self.xaxis_date = dict(rangeslider=dict(visible=True), type='date', tickformat=tick_fmt)
        self.xaxis_cat = dict(rangeslider=dict(visible=False), type='category')

    def get_summary_for_dashboard(self, month_offset):
        idx = self.today.month - 1 + month_offset
        year, month = self.today.year + (idx // 12), (idx % 12) + 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        current_month_name = start.strftime('%B %Y')

        txs = [t for t in self.core_transactions if start <= t.date <= end]

        inc = sum(t.amount for t in txs if t.amount > 0 and t.category.name != EXCLUDED_CAT)
        exp = abs(sum(t.amount for t in txs if t.amount < 0 and t.category.name != EXCLUDED_CAT))
        s_in = sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount > 0 and t.category.name != EXCLUDED_CAT)
        s_out = abs(sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount < 0 and t.category.name != EXCLUDED_CAT))
        net_worth = 0.0
        balances = {}

        uncat = Transaction.query.join(Entity).filter(
            Entity.is_auto_created == True,
            Transaction.is_deleted == False,
            Transaction.account_id.notin_(self.brokerage_account_ids) if self.brokerage_account_ids else True
        ).count()
        dup_count = 0
        return {
            'start_date': start,
            'end_date': end,
            'current_month_name': current_month_name,
            'total_income': float(inc),
            'total_expense': float(exp),
            'net_worth': net_worth,
            'account_balances': balances,
            'uncategorized_count': uncat,
            'savings_in': float(s_in),
            'savings_out': float(s_out),
            'duplicate_count': dup_count,
            'data_anchor_date': self.today
        }

    def _get_period_key(self, date_obj):
        """Helper to normalize dates to the start of the period (Month 1st or Sunday)."""
        if self.view_mode == 'weekly':
            idx = (date_obj.weekday() + 1) % 7
            return date_obj - timedelta(days=idx)
        return date_obj.replace(day=1)

    def _get_event_overlays(self, start, end):
        relevant_events = [e for e in self.events if start <= e.date <= end]
        events_map = {}
        for e in relevant_events:
            key = self._get_period_key(e.date).strftime('%Y-%m-%d')
            if key not in events_map: events_map[key] = []
            events_map[key].append(e.description)
        shapes = [{'type': 'line', 'x0': k, 'x1': k, 'y0': 0, 'y1': 1, 'xref': 'x', 'yref': 'paper', 'line': {'color': '#9ca3af', 'width': 1.5, 'dash': 'dot'}} for k in events_map]
        anns = [{'x': k, 'y': 1.02, 'xref': 'x', 'yref': 'paper', 'text': "📍 " + "<br>📍 ".join(v), 'showarrow': False, 'xanchor': 'center', 'yanchor': 'bottom', 'font': {'size': 10, 'color': '#4b5563'}, 'align': 'center'} for k, v in events_map.items()]
        return shapes, anns

    def _group_transactions(self, txs, start, end, force_monthly=False):
        """Performance: Group transactions by period key once (O(N)) instead of filtering in loops (O(N^2))."""
        groups = {}
        for t in txs:
            if start <= t.date <= end:
                if force_monthly:
                    key = t.date.replace(day=1).strftime('%Y-%m-%d')
                else:
                    key = self._get_period_key(t.date).strftime('%Y-%m-%d')
                if key not in groups: groups[key] = []
                groups[key].append(t)
        return groups

    def _chart_eat_out_patterns(self, start_date, end_date):
        dow_totals = {i: 0.0 for i in range(7)}
        dow_counts = {i: 0 for i in range(7)}
        dow_payees = {i: {} for i in range(7)}
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        txs = [t for t in self.core_transactions
               if start_date <= t.date <= end_date
               and t.category.name == 'Eat Out']

        for t in txs:
            idx = t.date.weekday()
            dow_totals[idx] += float(abs(t.amount))
            dow_counts[idx] += 1
            payee_name = t.entity.name
            dow_payees[idx][payee_name] = dow_payees[idx].get(payee_name, 0) + 1

        y_vals = [dow_totals[i] for i in range(7)]
        avgs = [dow_totals[i]/dow_counts[i] if dow_counts[i] > 0 else 0 for i in range(7)]

        top_restaurants = []
        for i in range(7):
            if dow_payees[i]:
                top_payee = max(dow_payees[i].items(), key=lambda x: x[1])
                top_restaurants.append(f"👑 {top_payee[0]}")
            else:
                top_restaurants.append("")

        max_val = max(y_vals) if y_vals else 1
        colors = ['#ef4444' if val == max_val else '#6366f1' for val in y_vals]

        fig = go.Figure(data=[go.Bar(
            x=days,
            y=y_vals,
            marker_color=colors,
            text=y_vals,
            texttemplate='$%{y:,.0f}',
            textposition='auto',
            hovertemplate='<b>%{x}</b><br>Total: $%{y:,.2f}<br>Avg Ticket: $%{customdata:,.2f}<extra></extra>',
            customdata=avgs
        )])

        annotations = []
        for i, day in enumerate(days):
            if top_restaurants[i]:
                annotations.append({
                    'x': day,
                    'y': y_vals[i],
                    'text': top_restaurants[i],
                    'showarrow': False,
                    'yanchor': 'bottom',
                    'yshift': 10,
                    'font': {'size': 9, 'color': '#4b5563'}
                })

        fig.update_layout(
            title='Eat Out Spending by Day of Week',
            yaxis=dict(title='Total Spent ($)', tickformat="$,.0f"),
            xaxis=dict(title=''),
            margin=self.margin_std,
            annotations=annotations,
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def generate_all_charts(self):
        if self.view_mode == 'weekly':
            start_date = date(self.display_year, 1, 1)
            idx = (start_date.weekday() + 1) % 7
            start_date = start_date - timedelta(days=idx)
            end_date = date(self.display_year, 12, 31)
        else:
            start_date = date(self.display_year, 1, 1)
            end_date = date(self.display_year, 12, 31)

        periods = []
        curr = start_date
        while curr <= end_date:
            periods.append(curr)
            if self.view_mode == 'weekly':
                curr += timedelta(weeks=1)
            else:
                if curr.month == 12: curr = date(curr.year + 1, 1, 1)
                else: curr = date(curr.year, curr.month + 1, 1)

        period_strs = [p.strftime('%Y-%m-%d') for p in periods]

        monthly_periods = []
        monthly_start = date(self.display_year, 1, 1)
        monthly_end = date(self.display_year, 12, 31)
        curr_month = monthly_start
        while curr_month <= monthly_end:
            monthly_periods.append(curr_month)
            if curr_month.month == 12:
                curr_month = date(curr_month.year + 1, 1, 1)
            else:
                curr_month = date(curr_month.year, curr_month.month + 1, 1)
        monthly_period_strs = [p.strftime('%Y-%m-%d') for p in monthly_periods]

        grouped_core = self._group_transactions(self.core_transactions, start_date, end_date)
        grouped_core_monthly = self._group_transactions(self.core_transactions, monthly_start, monthly_end, force_monthly=True)

        shapes, anns = self._get_event_overlays(start_date, end_date)

        return {
            'chart_income_vs_expense': self._chart_income_vs_expense(periods, period_strs, grouped_core, shapes, anns),
            'chart_savings': self._chart_savings(periods, period_strs, grouped_core, shapes, anns),
            'chart_cash_flow': self._chart_cash_flow(periods, period_strs, grouped_core, shapes, anns),
            'chart_core_operating': self._chart_core_operating(periods, period_strs, grouped_core, shapes, anns),
            'chart_groceries': self._chart_groceries(periods, period_strs, grouped_core, shapes, anns),
            'chart_expense_broad': self._chart_expense_broad(periods, period_strs, grouped_core, shapes, anns),
            'chart_core_summary': self._chart_core_summary(periods, period_strs, grouped_core, shapes, anns),
            'chart_top_payees': self._chart_top_payees(self.core_transactions, start_date),
            'chart_yoy': self._chart_yoy(),
            'chart_core_breakdown': self._chart_core_breakdown(periods, period_strs, grouped_core, shapes, anns),
            'chart_hsa_activity': self._chart_hsa_activity(periods, period_strs, start_date, end_date),
            'chart_eat_out_patterns': self._chart_eat_out_patterns(start_date, end_date),
            'chart_brokerage_income': self._chart_brokerage_income(periods, period_strs, start_date, end_date),
            'chart_passive_coverage': self._chart_passive_income_coverage(periods, period_strs, start_date, end_date),
        }

    def _is_core_expense(self, t):
        """Check if a transaction is a 'core' expense."""
        return t.amount < 0 and t.category.name != EXCLUDED_CAT

    def _is_core_income(self, t):
        """Check if a transaction is 'core' income."""
        return t.amount > 0 and t.category.name != EXCLUDED_CAT

    def _chart_brokerage_income(self, periods, period_strs, start_date, end_date):
        """Passive income (interest + dividends) from brokerage account, stacked by symbol."""
        txs = [t for t in self.brokerage_transactions if start_date <= t.date <= end_date]

        def _display_name(raw):
            if len(raw) <= 5 and raw.replace('/', '').isalpha():
                return raw.upper()
            if 'TREAS' in raw.upper() or 'UNITED STATES' in raw.upper():
                return 'US Bond'
            return 'CD'

        entities_data = {}
        for t in txs:
            name = _display_name(t.entity.name)
            p_key = self._get_period_key(t.date).strftime('%Y-%m-%d')
            if name not in entities_data:
                entities_data[name] = {ps: 0.0 for ps in period_strs}
            if p_key in entities_data[name]:
                entities_data[name][p_key] += float(t.amount)

        display_year = self.display_year
        year_total = sum(float(t.amount) for t in self.brokerage_all_txs if t.date.year == display_year)
        prior_total = sum(float(t.amount) for t in self.brokerage_all_txs if t.date.year < display_year)

        all_time_total = year_total + prior_total
        is_current_year = date.today().year == display_year
        year_label = f"{display_year} YTD" if is_current_year else str(display_year)
        title = f"Brokerage Passive Income — {year_label}: ${year_total:,.0f}"
        if all_time_total > year_total:
            title += f" | All-Time Total: ${all_time_total:,.0f}"

        FIXED_COLORS = {
            'BND':     '#3b82f6',
            'CD':      '#f97316',
            'FXNAX':   '#8b5cf6',
            'FSBC':    '#ec4899',
            'HMBGX':   '#eab308',
            'HMBD':    '#14b8a6',
            'SPAXX':   '#ef4444',
            'SCHD':    '#84cc16',
            'US Bond': '#10b981',
            'VTI':     '#a855f7',
            'VOO':     '#0ea5e9',
            'VXUS':    '#fb923c',
        }
        FALLBACK = [
            '#f43f5e', '#22c55e', '#d946ef', '#2dd4bf',
            '#facc15', '#60a5fa', '#e879f9', '#4ade80',
        ]

        def _entity_color(n):
            if n in FIXED_COLORS:
                return FIXED_COLORS[n]
            return FALLBACK[sum(ord(c) for c in n) % len(FALLBACK)]

        traces = []
        for name, monthly_data in sorted(entities_data.items(), reverse=True):
            y_vals = [monthly_data.get(ps, 0.0) for ps in period_strs]
            traces.append(go.Bar(
                name=name, x=period_strs, y=y_vals,
                marker_color=_entity_color(name)
            ))

        fig = go.Figure(data=traces)
        fig.update_layout(
            title=title,
            barmode='stack',
            yaxis=dict(title='Income ($)', tickformat='$,.0f'),
            xaxis=self.xaxis_date,
            showlegend=True,
            legend=dict(orientation='h', yanchor='top', y=-0.3, xanchor='center', x=0.5),
            margin=self.margin_legend,
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def _chart_hsa_activity(self, periods, period_strs, start_date, end_date):
        data_by_payee = {}
        txs = [t for t in self.hsa_transactions if start_date <= t.date <= end_date and t.amount < 0]
        for t in txs:
            name = t.entity.name
            m_key = self._get_period_key(t.date).strftime('%Y-%m-%d')
            if name not in data_by_payee: data_by_payee[name] = {}
            data_by_payee[name][m_key] = data_by_payee[name].get(m_key, 0.0) + float(abs(t.amount))
        traces = []
        for name in sorted(data_by_payee.keys(), reverse=True):
            y_vals = [data_by_payee[name].get(m, 0.0) for m in period_strs]
            traces.append(go.Bar(name=name, x=period_strs, y=y_vals))
        fig = go.Figure(data=traces)
        fig.update_layout(title='HSA Expenses by Payee', yaxis=dict(title='Spent ($)', tickformat="$,.0f"), xaxis=self.xaxis_date, barmode='stack', margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_core_breakdown(self, periods, period_strs, grouped_txs, shapes, anns):
        cat_data = {}
        all_cats = set()
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            if p_str not in cat_data: cat_data[p_str] = {}
            temp_cat_totals = {}
            for t in p_txs:
                if self._is_core_expense(t):
                    c_name = t.category.name
                    all_cats.add(c_name)
                    temp_cat_totals[c_name] = temp_cat_totals.get(c_name, 0.0) + float(t.amount)
            for c, val in temp_cat_totals.items():
                cat_data[p_str][c] = abs(val) if val < 0 else 0.0
        fig = go.Figure()
        for cat in sorted(list(all_cats), reverse=True):
            y_vals = [cat_data.get(m, {}).get(cat, 0) for m in period_strs]
            fig.add_trace(go.Bar(name=cat, x=period_strs, y=y_vals))
        fig.update_layout(title='Core Expenses by Category (Breakdown)', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, legend=dict(traceorder='reversed'), **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_income_vs_expense(self, periods, period_strs, grouped_txs, shapes, anns):
        """Building Wealth (Excl. Investments)"""
        regular_inc, investment_withdrawals, c_exp = [], [], []
        cumulative_surplus = 0.0
        cumulative_net = []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            regular_inc_val = float(sum(t.amount for t in p_txs if t.amount > 0 and t.category.name != EXCLUDED_CAT and t.category.name != 'Investment'))
            invest_withdrawal_val = float(sum(t.amount for t in p_txs if t.amount > 0 and t.category.name == 'Investment'))
            exp_val = float(abs(sum(t.amount for t in p_txs if t.amount < 0 and t.category.name != EXCLUDED_CAT and t.category.name != 'Investment')))
            current_surplus = regular_inc_val - exp_val
            cumulative_surplus += current_surplus
            regular_inc.append(regular_inc_val)
            investment_withdrawals.append(invest_withdrawal_val)
            c_exp.append(exp_val)
            cumulative_net.append(cumulative_surplus)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Investment Withdrawals', x=period_strs, y=investment_withdrawals, marker_color='#ef4444'))
        fig.add_trace(go.Bar(name='Income', x=period_strs, y=regular_inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Expenses', x=period_strs, y=c_exp, marker_color='#6366f1'))
        fig.add_trace(go.Scatter(name='Cumulative Surplus', x=period_strs, y=cumulative_net, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(
            title='In with Investments vs Out w/o Investments',
            barmode='group',
            yaxis=dict(title='$', tickformat="$,.0f"),
            xaxis=self.xaxis_date,
            showlegend=True,
            legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5),
            margin=self.margin_legend,
            shapes=shapes,
            annotations=anns,
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def _chart_savings(self, periods, period_strs, grouped_txs, shapes, anns):
        net_vals, hover_txt, colors = [], [], []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            p_txs = [t for t in p_txs if t.account_id in self.savings_account_ids and t.category.name != EXCLUDED_CAT]
            in_val = float(sum(t.amount for t in p_txs if t.amount > 0))
            out_val = float(abs(sum(t.amount for t in p_txs if t.amount < 0)))
            net = in_val - out_val
            net_vals.append(net)
            colors.append('#10b981' if net >= 0 else '#ef4444')
            hover_txt.append(f"Net: ${net:,.2f}<br>In: ${in_val:,.2f}<br>Out: ${out_val:,.2f}")
        fig = go.Figure(go.Bar(x=period_strs, y=net_vals, marker_color=colors, text=net_vals, texttemplate='$%{y:,.0f}', textposition='auto', hoverinfo='text', hovertext=hover_txt))
        fig.update_layout(title='Net Savings Flow', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_cash_flow(self, periods, period_strs, grouped_txs, shapes, anns):
        net_vals, colors = [], []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            p_txs = [t for t in p_txs if t.category.name != EXCLUDED_CAT]
            inc = float(sum(t.amount for t in p_txs if t.amount > 0))
            exp = float(abs(sum(t.amount for t in p_txs if t.amount < 0)))
            net = inc - exp
            net_vals.append(net)
            colors.append('#10b981' if net >= 0 else '#ef4444')
        fig = go.Figure(go.Bar(x=period_strs, y=net_vals, marker_color=colors, text=net_vals, texttemplate='$%{y:,.0f}', textposition='auto'))
        fig.update_layout(title='Net Cash Flow', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_core_summary(self, periods, period_strs, grouped_txs, shapes, anns):
        payee_map = {}
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            for t in p_txs:
                if self._is_core_expense(t):
                    name = t.entity.name
                    payee_map[name] = payee_map.get(name, 0.0) + float(t.amount)
        sorted_payees = sorted(payee_map.items(), key=lambda item: item[1])[:20]
        sorted_payees = sorted_payees[::-1]
        names = [p[0] for p in sorted_payees]
        vals = [abs(p[1]) for p in sorted_payees]
        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#6366f1', text=vals, texttemplate='$%{x:,.0f}', hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>')])
        fig.update_layout(title='Top 20 Core Operating Payees (Current View)', xaxis=dict(title='Total Spent', tickformat="$,.0f"), margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_core_operating(self, periods, period_strs, grouped_txs, shapes, anns):
        c_inc, c_exp = [], []
        cumulative_surplus = 0.0
        cumulative_net = []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            inc_val = float(sum(t.amount for t in p_txs if self._is_core_income(t)))
            exp_val = float(abs(sum(t.amount for t in p_txs if self._is_core_expense(t))))
            current_surplus = inc_val - exp_val
            cumulative_surplus += current_surplus
            c_inc.append(inc_val)
            c_exp.append(exp_val)
            cumulative_net.append(cumulative_surplus)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Full Income', x=period_strs, y=c_inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Full Expenses', x=period_strs, y=c_exp, marker_color='#6366f1'))
        fig.add_trace(go.Scatter(name='Cumulative Surplus', x=period_strs, y=cumulative_net, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Full Income vs Full Expense', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=self.margin_legend, shapes=shapes, annotations=anns, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_groceries(self, periods, period_strs, grouped_txs, shapes, anns):
        groc, dine = [], []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            groc.append(float(abs(sum(t.amount for t in p_txs if t.category.name == 'Groceries'))))
            dine.append(float(abs(sum(t.amount for t in p_txs if t.category.name == 'Eat Out'))))
        nonzero_groc = [v for v in groc if v > 0]
        nonzero_dine = [v for v in dine if v > 0]
        avg_groc = sum(nonzero_groc) / len(nonzero_groc) if nonzero_groc else 0
        avg_dine = sum(nonzero_dine) / len(nonzero_dine) if nonzero_dine else 0
        traces = [
            go.Bar(name='Groceries', x=period_strs, y=groc, marker_color='#10b981'),
            go.Bar(name='Eat Out', x=period_strs, y=dine, marker_color='#f59e0b'),
            go.Scatter(name='Avg Groceries', x=period_strs, y=[avg_groc]*len(period_strs), mode='lines', line=dict(color='#10b981', width=2, dash='dash'), opacity=0.7),
            go.Scatter(name='Avg Eat Out', x=period_strs, y=[avg_dine]*len(period_strs), mode='lines', line=dict(color='#f59e0b', width=2, dash='dash'), opacity=0.7)
        ]
        fig = go.Figure(data=traces)
        fig.update_layout(title='Groceries vs. Eating Out', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_top_payees(self, txs, start_date):
        income_map = {}
        for t in txs:
            name = t.entity.name
            if (t.date >= start_date and
                    t.category.name != EXCLUDED_CAT and
                    t.amount > 0):
                income_map[name] = income_map.get(name, 0.0) + float(t.amount)
        sorted_sources = sorted(income_map.items(), key=lambda item: item[1], reverse=True)[:10]
        sorted_sources = sorted_sources[::-1]
        names = [p[0] for p in sorted_sources]
        vals = [p[1] for p in sorted_sources]
        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#10b981', text=vals, texttemplate='$%{x:,.0f}', hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>')])
        fig.update_layout(title='Top 10 Income Sources (Current View)', xaxis=dict(title='Total Income', tickformat="$,.0f"), margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_yoy(self):
        years_to_show = [self.today.year - 2, self.today.year - 1, self.today.year]
        colors = ['#9ca3af', '#f59e0b', '#6366f1']
        fig = go.Figure()
        for i, yr in enumerate(years_to_show):
            yr_txs = [t for t in self.core_transactions if t.date.year == yr]
            monthly_totals = {}
            for t in yr_txs:
                p_name = t.entity.name
                if 'JEA' in p_name.upper() and t.amount < 0:
                    m = t.date.month
                    monthly_totals[m] = monthly_totals.get(m, 0.0) + float(t.amount)
            y_vals = []
            for m in range(1, 13):
                val = monthly_totals.get(m, 0.0)
                if yr == self.today.year and m > self.today.month:
                    y_vals.append(None)
                else:
                    y_vals.append(abs(val))
            fig.add_trace(go.Bar(name=str(yr), x=list(calendar.month_name[1:]), y=y_vals, marker_color=colors[i], text=y_vals, texttemplate='$%{y:,.0f}', hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>'))
        fig.update_layout(title='YoY JEA Expenses (3-Year Comparison)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_cat, margin=self.margin_std, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1), **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_expense_broad(self, periods, period_strs, grouped_txs, shapes, anns):
        """Savings Rate Trend - shows percentage of income saved each period"""
        savings_rates = []
        colors = []
        hover_texts = []

        SAVINGS_CATS = {'Savings Transfer', 'Investment Transfer', 'Investment', 'VUL'}
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            p_txs = [t for t in p_txs if t.category.name != EXCLUDED_CAT]

            total_income = sum(t.amount for t in p_txs if t.amount > 0)
            total_expenses = abs(sum(t.amount for t in p_txs if t.amount < 0 and t.category.name not in SAVINGS_CATS))
            net_savings = total_income - total_expenses

            if total_income > 0:
                savings_rate = (net_savings / total_income) * 100
            else:
                savings_rate = 0

            savings_rates.append(savings_rate)
            colors.append('#10b981' if savings_rate >= 20 else '#f59e0b' if savings_rate >= 10 else '#ef4444')
            hover_texts.append(f"Rate: {savings_rate:.1f}%<br>Income: ${total_income:,.0f}<br>Expenses: ${total_expenses:,.0f}<br>Saved: ${net_savings:,.0f}")

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=period_strs,
            y=savings_rates,
            marker_color=colors,
            text=[f"{rate:.1f}%" for rate in savings_rates],
            textposition='auto',
            hoverinfo='text',
            hovertext=hover_texts,
            name='Savings Rate'
        ))
        fig.add_trace(go.Scatter(
            x=period_strs,
            y=[20]*len(period_strs),
            mode='lines',
            line=dict(color='#10b981', width=2, dash='dash'),
            name='Good (20%)',
            opacity=0.5
        ))
        fig.add_trace(go.Scatter(
            x=period_strs,
            y=[50]*len(period_strs),
            mode='lines',
            line=dict(color='#6366f1', width=2, dash='dash'),
            name='Excellent (50%)',
            opacity=0.5
        ))
        fig.update_layout(
            title='Savings Rate Trend',
            yaxis=dict(title='Savings Rate (%)', tickformat=".0f", range=[min(0, min(savings_rates) - 5) if savings_rates else 0, max(60, max(savings_rates) + 5) if savings_rates else 60]),
            xaxis=self.xaxis_date,
            shapes=shapes,
            annotations=anns,
            margin=self.margin_events,
            showlegend=True,
            legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5),
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def _chart_passive_income_coverage(self, periods, period_strs, start_date, end_date):
        """Brokerage passive income as % of core monthly expenses."""
        brok_by_period = {}
        for t in self.brokerage_transactions:
            if start_date <= t.date <= end_date and t.amount > 0:
                key = self._get_period_key(t.date).strftime('%Y-%m-%d')
                brok_by_period[key] = brok_by_period.get(key, 0.0) + float(t.amount)

        SAVINGS_CATS = {'Savings Transfer', 'Investment Transfer', 'Investment', 'VUL'}
        core_exp_by_period = {}
        for t in self.core_transactions:
            if start_date <= t.date <= end_date and t.amount < 0 and t.category.name != EXCLUDED_CAT and t.category.name not in SAVINGS_CATS:
                key = self._get_period_key(t.date).strftime('%Y-%m-%d')
                core_exp_by_period[key] = core_exp_by_period.get(key, 0.0) + abs(float(t.amount))

        pcts, colors, hover = [], [], []
        for p_str in period_strs:
            income = brok_by_period.get(p_str, 0.0)
            expenses = core_exp_by_period.get(p_str, 0.0)
            pct = round(income / expenses * 100, 1) if expenses > 0 else 0.0
            pcts.append(pct)
            colors.append('#10b981' if pct >= 100 else '#3b82f6' if pct >= 50 else '#f97316')
            hover.append(f"Coverage: {pct:.1f}%<br>Passive Income: ${income:,.2f}<br>Core Expenses: ${expenses:,.2f}")

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=period_strs, y=pcts,
            marker_color=colors,
            text=[f'{p:.1f}%' for p in pcts],
            textposition='outside',
            hoverinfo='text',
            hovertext=hover,
            name='Coverage %'
        ))
        fig.add_hline(y=100, line_dash='dash', line_color='#ef4444', line_width=1.5,
                      annotation_text='100% covered', annotation_position='right')
        fig.update_layout(
            title='Passive Income Coverage Ratio (Brokerage Income / Core Expenses)',
            yaxis=dict(title='Coverage (%)', ticksuffix='%', rangemode='tozero'),
            xaxis=self.xaxis_date,
            margin=self.margin_events,
            **self.base_layout
        )
        return to_json(fig, pretty=True)
