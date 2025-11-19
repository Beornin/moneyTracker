import os
import calendar
import re
from datetime import datetime, date, timedelta 
import pdfplumber 
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, extract, case, or_
from sqlalchemy.orm import joinedload
import plotly.graph_objects as go
from plotly.io import to_json 
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://budget_user:ben@localhost:5432/budget_db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24)

db = SQLAlchemy(app)

# --- CONSTANTS ---
EXCLUDED_CAT = 'Transfer Credit Card Payment'
CHASE_DATE_REGEX = re.compile(r"Opening/Closing Date\s*([\d/]+)\s*-\s*([\d/]+)")
CHASE_TRANS_REGEX = re.compile(r"(\d{1,2}/\d{1,2})\s+(.+)\s+([\d,.-]+\.\d{2})")

# --- MODELS ---

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    account_type = db.Column(db.String(50), nullable=False) 
    starting_balance = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)
    transactions = db.relationship('Transaction', backref='account', lazy=True, cascade="all, delete-orphan")
    
    __table_args__ = (
        db.UniqueConstraint('name', 'account_type', name='_account_uc'),
    )

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    type = db.Column(db.String(20), nullable=False)
    transactions = db.relationship('Transaction', backref='category', lazy=True)
    payee_rules = db.relationship('PayeeRule', backref='category', lazy=True)
    
    # Index for faster lookups during import
    __table_args__ = (
        db.Index('idx_category_name', 'name'),
    )

class PayeeRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fragment = db.Column(db.String(500), nullable=False, unique=True)
    display_name = db.Column(db.String(500), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    payees = db.relationship('Payee', backref='rule', lazy=True)
    
    __table_args__ = (
        db.Index('idx_rule_fragment', 'fragment'),
    )

class Payee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(500), nullable=False, unique=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('payee_rule.id'), nullable=True)
    transactions = db.relationship('Transaction', backref='payee', lazy=True)
    
    __table_args__ = (
        db.Index('idx_payee_name', 'name'),
    )

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    original_description = db.Column(db.String(500), nullable=True) 
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payee_id = db.Column(db.Integer, db.ForeignKey('payee.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    
    __table_args__ = (
        db.UniqueConstraint('date', 'original_description', 'amount', 'account_id', name='_unique_tx_uc'),
        # OPTIMIZATION: Indexes for dashboard filtering
        db.Index('idx_tx_date_deleted', 'date', 'is_deleted'),
        db.Index('idx_tx_category', 'category_id'),
        db.Index('idx_tx_account', 'account_id'),
    )

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500), nullable=False)
    __table_args__ = (db.UniqueConstraint('date', 'description', name='_event_uc'),)

# --- INITIALIZATION ---

def create_tables():
    db.create_all()
    if Category.query.count() == 0:
        uncat = Category(name='Uncategorized', type='Expense')
        db.session.add(uncat)
        db.session.commit() 
        initial_cats = [
            {'name': 'Salary', 'type': 'Income'}, {'name': 'Investment Income', 'type': 'Income'},
            {'name': 'Groceries', 'type': 'Expense'}, {'name': 'Dining Out', 'type': 'Expense'},
            {'name': 'Housing', 'type': 'Expense'}, {'name': 'Utilities', 'type': 'Expense'},
            {'name': 'Transportation', 'type': 'Expense'}, {'name': 'Medical', 'type': 'Expense'},
            {'name': 'Shopping', 'type': 'Expense'}, {'name': 'Entertainment', 'type': 'Expense'},
            {'name': 'Credit Card Payment', 'type': 'Transfer'}, {'name': 'Savings Transfer', 'type': 'Transfer'},
        ]
        for c in initial_cats: db.session.add(Category(**c))
        db.session.commit()

# --- LOGIC HELPERS ---

def get_uncategorized_id():
    return Category.query.filter_by(name='Uncategorized').first().id

def apply_rules_to_payee(payee, uncat_id):
    if payee.rule_id: return payee.rule.category_id
    rules = PayeeRule.query.all()
    name_upper = payee.name.upper()
    for r in rules:
        if r.fragment in name_upper:
            payee.rule_id = r.id
            db.session.commit()
            return r.category_id
    return uncat_id

def run_rule_on_all_payees(rule, overwrite=False):
    uncat_id = get_uncategorized_id()
    if overwrite: candidates = Payee.query.all()
    else: candidates = Payee.query.filter_by(rule_id=None).all()
    
    if not candidates: return 0
    
    matches = [p.id for p in candidates if rule.fragment in p.name.upper()]
    if not matches: return 0

    Payee.query.filter(Payee.id.in_(matches)).update({'rule_id': rule.id}, synchronize_session=False)
    Transaction.query.filter(Transaction.payee_id.in_(matches)).update({'category_id': rule.category_id}, synchronize_session=False)
    
    db.session.commit()
    return len(matches)

def parse_chase_pdf(file_stream):
    transactions = []
    try:
        with pdfplumber.open(file_stream) as pdf:
            page1_text = pdf.pages[0].extract_text()
            date_match = CHASE_DATE_REGEX.search(page1_text)
            if not date_match: return None
            end_date = datetime.strptime(date_match.group(2), '%m/%d/%y').date()
            end_year, end_month = end_date.year, end_date.month
            full_text = "".join([p.extract_text() for p in pdf.pages])
            matches = CHASE_TRANS_REGEX.findall(full_text)
            for m in matches:
                d_str, desc, amt_str = m
                if "Order Number" in desc: continue
                t_month = int(d_str.split('/')[0])
                year = end_year if t_month <= end_month else end_year - 1
                dt = datetime.strptime(f"{t_month}/{d_str.split('/')[1]}/{year}", '%m/%d/%Y').date()
                try: transactions.append({'Date': dt, 'Description': desc.strip(), 'Amount': float(amt_str.replace(',',''))})
                except: continue
        return transactions
    except: return None

def get_monthly_summary_direct(month_offset):
    """
    Standalone function for pages that need summary without loading 24mo history.
    Used by: edit_transactions
    """
    today = date.today()
    idx = today.month - 1 + month_offset
    year, month = today.year + (idx // 12), (idx % 12) + 1
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    
    current_month_name = start.strftime('%B %Y')
    
    # Only date math needed for header context
    return {
        'start_date': start, 
        'end_date': end, 
        'current_month_name': current_month_name
    }

# --- DASHBOARD SERVICE (OPTIMIZED) ---

class DashboardService:
    """Generates all charts using efficient in-memory aggregation."""
    
    def __init__(self):
        self.today = date.today()
        # Fetch 24 months of data once to support YoY and 12-mo trends
        self.start_date_24mo = date(self.today.year - 2, self.today.month, 1)
        
        # Eager load relations to prevent N+1 queries
        self.transactions = Transaction.query.options(
            joinedload(Transaction.category),
            joinedload(Transaction.account),
            joinedload(Transaction.payee).joinedload(Payee.rule)
        ).filter(
            Transaction.date >= self.start_date_24mo,
            Transaction.is_deleted == False
        ).all()

        self.events = Event.query.filter(
            Event.date >= self.start_date_24mo
        ).all()

        self.savings_account_ids = {a.id for a in Account.query.filter_by(account_type='savings').all()}
        
        # --- GLOBAL STYLES ---
        self.base_layout = dict(
            paper_bgcolor='rgba(0,0,0,0)', 
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#71717a'), 
            autosize=True
        )
        
        self.margin_std = dict(t=40, b=20, l=50, r=10)
        self.margin_legend = dict(t=40, b=100, l=50, r=10) 
        self.margin_events = dict(t=60, b=20, l=50, r=10)
        
        self.xaxis_date = dict(rangeslider=dict(visible=True), type='date', tickformat='%b %Y')
        self.xaxis_cat = dict(rangeslider=dict(visible=False), type='category')

    def get_summary_for_dashboard(self, month_offset):
        """
        OPTIMIZATION: Calculates summary stats from ALREADY FETCHED data in memory.
        Replaces the need for a separate SQL query on the index page.
        """
        idx = self.today.month - 1 + month_offset
        year, month = self.today.year + (idx // 12), (idx % 12) + 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])

        # In-memory filtering (Extremely fast compared to DB roundtrip)
        txs = [t for t in self.transactions if start <= t.date <= end]
        
        inc = sum(t.amount for t in txs if t.category.type == 'Income' and t.category.name != EXCLUDED_CAT)
        exp = abs(sum(t.amount for t in txs if t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT))
        
        # Savings logic
        s_in = sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount > 0 and (t.category.type in ['Transfer', 'Income']))
        s_out = abs(sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount < 0 and t.category.type == 'Transfer'))

        # Totals still need DB for "All Time" balances, but that's cheap
        net_worth = 0.0
        balances = {}
        for a in Account.query.all():
            net = db.session.query(func.sum(Transaction.amount)).filter(
                Transaction.account_id==a.id, Transaction.is_deleted==False
            ).scalar() or 0
            bal = float(a.starting_balance) + float(net)
            balances[a.name] = bal
            net_worth += bal

        uncat = Transaction.query.join(Payee).filter(Payee.rule_id == None, Transaction.is_deleted == False).count()

        return {
            'start_date': start, 'end_date': end, 'current_month_name': start.strftime('%B %Y'),
            'total_income': float(inc), 'total_expense': float(exp), 'net_worth': net_worth,
            'account_balances': balances, 'uncategorized_count': uncat,
            'savings_in': float(s_in), 'savings_out': float(s_out)
        }

    def _get_event_overlays(self, start, end):
        relevant_events = [e for e in self.events if start <= e.date <= end]
        events_map = {}
        for e in relevant_events:
            # Map event to the 1st of the month for chart alignment
            key = e.date.replace(day=1).strftime('%Y-%m-%d')
            if key not in events_map: events_map[key] = []
            events_map[key].append(e.description)

        shapes, anns = [], []
        for key, descs in events_map.items():
            label = "📍 " + "<br>📍 ".join(descs)
            shapes.append({
                'type': 'line', 'x0': key, 'x1': key, 'y0': 0, 'y1': 1,
                'xref': 'x', 'yref': 'paper', 'line': {'color': '#9ca3af', 'width': 1.5, 'dash': 'dot'}
            })
            anns.append({
                'x': key, 'y': 1.02, 'xref': 'x', 'yref': 'paper',
                'text': label, 'showarrow': False, 'xanchor': 'center', 'yanchor': 'bottom',
                'font': {'size': 10, 'color': '#4b5563'}, 'align': 'center'
            })
        return shapes, anns

    def generate_all_charts(self):
        y_start, m_start = self.today.year, self.today.month - 11
        while m_start <= 0: m_start += 12; y_start -= 1
        s12 = date(y_start, m_start, 1)
        e12 = date(self.today.year, self.today.month, calendar.monthrange(self.today.year, self.today.month)[1])
        
        # Get data for charts from cached transactions
        txs_12 = [t for t in self.transactions if s12 <= t.date <= e12]
        shapes, anns = self._get_event_overlays(s12, e12)

        months = []
        curr = s12
        while curr <= e12:
            months.append(curr)
            if curr.month == 12: curr = date(curr.year + 1, 1, 1)
            else: curr = date(curr.year, curr.month + 1, 1)
        
        # Use ISO format for Date Axis
        month_strs = [m.strftime('%Y-%m-%d') for m in months]

        return {
            'chart_income_vs_expense': self._chart_income_vs_expense(months, month_strs, txs_12, shapes, anns),
            'chart_savings': self._chart_savings(months, month_strs, txs_12, shapes, anns),
            'chart_cash_flow': self._chart_cash_flow(months, month_strs, txs_12, shapes, anns),
            'chart_core_operating': self._chart_core_operating(months, month_strs, txs_12, shapes, anns),
            'chart_groceries': self._chart_groceries(months, month_strs, txs_12, shapes, anns),
            'chart_expense_broad': self._chart_expense_broad(months, month_strs, txs_12, shapes, anns),
            'chart_top_payees': self._chart_top_payees(txs_12),
            'chart_yoy': self._chart_yoy(),
            'chart_flow_health': self._chart_flow_health(months, month_strs, txs_12, shapes, anns)
        }

    # --- INDIVIDUAL CHART LOGIC ---

    def _chart_income_vs_expense(self, months, month_strs, txs, shapes, anns):
        incs, exps = [], []
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.name != EXCLUDED_CAT]
            
            # Logic: Income + Fidelity Withdrawals
            curr_inc = sum(t.amount for t in m_txs if t.category.type == 'Income')
            curr_inc += sum(t.amount for t in m_txs if t.category.type == 'Transfer' and t.category.name in ['Transfer Fidelity', 'Transfer Money Market', 'Transfer External'] and t.amount > 0)
            
            incs.append(float(curr_inc))
            exps.append(float(abs(sum(t.amount for t in m_txs if t.category.type == 'Expense'))))

        fig = go.Figure(data=[
            go.Bar(name='Income', x=month_strs, y=incs, marker_color='#22c55e'),
            go.Bar(name='Expense', x=month_strs, y=exps, marker_color='#ef4444')
        ])
        fig.update_layout(title='Income vs Expenses (12 Mo)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_savings(self, months, month_strs, txs, shapes, anns):
        net_vals, hover_txt, colors = [], [], []
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.account_id in self.savings_account_ids and t.category.name != EXCLUDED_CAT]
            in_val = float(sum(t.amount for t in m_txs if t.amount > 0 and t.category.type in ['Transfer', 'Income']))
            out_val = float(abs(sum(t.amount for t in m_txs if t.amount < 0 and t.category.type == 'Transfer')))
            net = in_val - out_val
            net_vals.append(net)
            colors.append('#10b981' if net >= 0 else '#ef4444')
            hover_txt.append(f"Net: ${net:,.2f}<br>In: ${in_val:,.2f}<br>Out: ${out_val:,.2f}")

        fig = go.Figure(go.Bar(x=month_strs, y=net_vals, marker_color=colors, text=net_vals, texttemplate='$%{y:,.0f}', textposition='auto', hoverinfo='text', hovertext=hover_txt))
        fig.update_layout(title='Monthly Net Savings (Growth vs. Drawdown)', yaxis=dict(title='Net Change ($)', tickformat="$,.0f"), xaxis=self.xaxis_date, showlegend=False, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_cash_flow(self, months, month_strs, txs, shapes, anns):
        net_ops, cum_trend, colors, hover_txt = [], [], [], []
        run_total = 0.0
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.name != EXCLUDED_CAT]
            inf = float(sum(t.amount for t in m_txs if t.amount > 0))
            out = float(abs(sum(t.amount for t in m_txs if t.amount < 0)))
            net = inf - out
            run_total += net
            net_ops.append(net)
            cum_trend.append(run_total)
            colors.append('#10b981' if net >= 0 else '#ef4444')
            hover_txt.append(f"Net: ${net:,.0f}<br>In: ${inf:,.0f}<br>Out: ${out:,.0f}")

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Monthly Net', x=month_strs, y=net_ops, marker_color=colors, text=net_ops, texttemplate='$%{y:,.0f}', textposition='auto', opacity=0.6, hoverinfo='text', hovertext=hover_txt))
        fig.add_trace(go.Scatter(name='Cumulative Cash Trend', x=month_strs, y=cum_trend, mode='lines+markers', line=dict(color='#f59e0b', width=4), marker=dict(size=6)))
        fig.update_layout(title='Total Cash Flow (Excluding CC Payments)', yaxis=dict(title='Net Change ($)', tickformat="$,.0f"), xaxis=self.xaxis_date, showlegend=False, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_core_operating(self, months, month_strs, txs, shapes, anns):
        c_inc, c_exp, net = [], [], []
        excl = ['Car Payment', 'Insurance']
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month]
            inc_val = float(sum(t.amount for t in m_txs if t.category.type == 'Income'))
            exp_val = float(abs(sum(t.amount for t in m_txs if t.category.type == 'Expense' and t.category.name not in excl)))
            c_inc.append(inc_val)
            c_exp.append(exp_val)
            net.append(inc_val - exp_val)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Core Income', x=month_strs, y=c_inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Core Expenses', x=month_strs, y=c_exp, marker_color='#6366f1'))
        fig.add_trace(go.Scatter(name='Operating Surplus', x=month_strs, y=net, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(
            title='Core Operating Performance (Excl. Car & Insurance)', barmode='group', 
            yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date,
            showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5),
            margin=self.margin_legend, shapes=shapes, annotations=anns, **self.base_layout
        )
        return to_json(fig, pretty=True)

    def _chart_groceries(self, months, month_strs, txs, shapes, anns):
        groc, dine = [], []
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month]
            groc.append(float(abs(sum(t.amount for t in m_txs if t.category.name == 'Groceries'))))
            dine.append(float(abs(sum(t.amount for t in m_txs if t.category.name == 'Eat Out'))))
        fig = go.Figure([
            go.Bar(name='Groceries', x=month_strs, y=groc, marker_color='#10b981'),
            go.Bar(name='Eat Out', x=month_strs, y=dine, marker_color='#f59e0b')
        ])
        fig.update_layout(title='Groceries vs. Eating Out (12 Mo)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_expense_broad(self, months, month_strs, txs, shapes, anns):
        cat_data = {}
        all_cats = set()
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT]
            m_key = m.strftime('%Y-%m-%d')
            if m_key not in cat_data: cat_data[m_key] = {}
            for t in m_txs:
                c_name = t.category.name
                all_cats.add(c_name)
                cat_data[m_key][c_name] = cat_data[m_key].get(c_name, 0) + float(abs(t.amount))

        fig = go.Figure()
        for cat in sorted(list(all_cats)):
            y_vals = [cat_data.get(m, {}).get(cat, 0) for m in month_strs]
            fig.add_trace(go.Bar(name=cat, x=month_strs, y=y_vals))

        fig.update_layout(title='Expenses by Category (12 Mo)', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_top_payees(self, txs):
        payee_totals = {}
        for t in txs:
            if t.date >= (self.today - timedelta(days=365)) and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT:
                name = t.payee.rule.display_name if t.payee.rule else t.payee.name
                payee_totals[name] = payee_totals.get(name, 0) + float(t.amount)
        
        sorted_payees = sorted(payee_totals.items(), key=lambda item: item[1])[:20]
        sorted_payees = sorted_payees[::-1] # Reverse for visual
        names = [p[0] for p in sorted_payees]
        vals = [abs(p[1]) for p in sorted_payees]

        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#6366f1', text=vals, texttemplate='$%{x:,.0f}')])
        fig.update_layout(title='Top 20 Payees (Last 12 Mo)', xaxis=dict(title='Total Spent', tickformat="$,.0f"), margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_yoy(self):
        curr_yr, last_yr = self.today.year, self.today.year - 1
        def get_yr_data(yr):
            monthly = [0.0] * 13
            yr_txs = [t for t in self.transactions if t.date.year == yr and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT]
            for t in yr_txs: monthly[t.date.month] += float(abs(t.amount))
            return monthly[1:]

        curr_data = get_yr_data(curr_yr)
        last_data = get_yr_data(last_yr)
        curr_data = [d if i < self.today.month else None for i, d in enumerate(curr_data)]

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=list(calendar.month_name[1:]), y=last_data, mode='lines+markers', name=f'{last_yr}', line=dict(color='gray', dash='dash')))
        fig.add_trace(go.Scatter(x=list(calendar.month_name[1:]), y=curr_data, mode='lines+markers', name=f'{curr_yr}', line=dict(color='#6366f1', width=3)))
        fig.update_layout(title='YoY Expenses', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_cat, margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_flow_health(self, months, month_strs, txs, shapes, anns):
        good_kw = ['checking->savings', 'to savings', 'to fidelity', 'checking->fidelity']
        bad_kw = ['savings->checking', 'from savings', 'fidelity->savings', 'fidelity->checking', 'from fidelity']
        strat_kw = ['car payoff', 'strategic', 'large purchase']
        up, down = [], []
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.type == 'Transfer' and t.category.name != EXCLUDED_CAT]
            g, b = 0.0, 0.0
            for t in m_txs:
                name = t.category.name.lower()
                amt = abs(float(t.amount))
                if any(k in name for k in strat_kw): continue
                if any(k in name for k in bad_kw): b += amt
                elif any(k in name for k in good_kw): g += amt
            up.append(g); down.append(-b)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Building Reserves', x=month_strs, y=up, marker_color='#10b981', text=up, texttemplate='$%{y:,.0f}', textposition='auto'))
        fig.add_trace(go.Bar(name='Tapping Reserves', x=month_strs, y=down, marker_color='#ef4444', text=down, texttemplate='$%{y:,.0f}', textposition='auto'))
        fig.update_layout(title='Directional Flow (Reserve Building vs. Tapping)', yaxis=dict(title='Flow Volume ($)', tickformat="$,.0f"), barmode='relative', xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

# --- AI INSIGHTS LOGIC ---

def get_spending_data_for_period(start, end):
    transactions = Transaction.query.join(Category).filter(
        Transaction.date >= start, Transaction.date <= end, Transaction.is_deleted == False
    ).all()
    spending_by_cat = {}
    total_income = 0
    for t in transactions:
        if t.category.type == 'Expense':
            spending_by_cat[t.category.name] = spending_by_cat.get(t.category.name, 0) + abs(float(t.amount))
        elif t.category.type == 'Income':
            total_income += float(t.amount)
    for c, v in spending_by_cat.items(): spending_by_cat[c] = round(v, 2)
    total_expense = sum(spending_by_cat.values())
    return spending_by_cat, round(total_income - total_expense, 2)

@app.route('/api/insight_data')
def api_insight_data():
    try:
        today = date.today()
        lm_end = today.replace(day=1) - timedelta(days=1)
        lm_start = lm_end.replace(day=1)
        pm_end = lm_start - timedelta(days=1)
        pm_start = pm_end.replace(day=1)
        lm_s, lm_n = get_spending_data_for_period(lm_start, lm_end)
        pm_s, pm_n = get_spending_data_for_period(pm_start, pm_end)
        return jsonify({"last_month_name": lm_start.strftime('%B %Y'), "last_month_spending": lm_s, "prior_month_spending": pm_s, "last_month_net": lm_n, "prior_month_net": pm_n})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/yearly_insight_data')
def api_yearly_insight_data():
    try:
        today = date.today()
        c_start, c_end = today.replace(day=1, month=1), today
        l_end = today.replace(year=today.year - 1)
        l_start = l_end.replace(day=1, month=1)
        cs, cn = get_spending_data_for_period(c_start, c_end)
        ls, ln = get_spending_data_for_period(l_start, l_end)
        return jsonify({"current_ytd_spending": cs, "last_ytd_spending": ls, "current_ytd_net": cn, "last_ytd_net": ln, "current_year": today.year, "last_year": today.year - 1})
    except Exception as e: return jsonify({'error': str(e)}), 500

# --- ROUTES ---

@app.route('/', defaults={'month_offset': '0'})
@app.route('/<string:month_offset>')
def index(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    
    # OPTIMIZATION: Instantiate Service ONCE
    service = DashboardService()
    
    # 1. Get Monthly Summary (Cards) from MEMORY (No SQL)
    summary = service.get_summary_for_dashboard(month_offset)
    
    # 2. Get All Charts from MEMORY (No SQL)
    charts = service.generate_all_charts()
    
    return render_template('index.html', 
                           month_offset=month_offset, 
                           summary=summary,
                           accounts=Account.query.all(), 
                           gemini_api_key=os.getenv('GEMINI_API_KEY'),
                           **charts)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    account_id = request.form.get('account_id')
    files = request.files.getlist('file')
    if not account_id or not files: return redirect(url_for('index'))
    account = db.session.get(Account, account_id)
    if not account: abort(404)
    uncat_id = get_uncategorized_id()
    added, skipped = 0, 0
    for file in files:
        try:
            df = pd.DataFrame()
            if file.filename.lower().endswith('.csv'):
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                df = pd.read_csv(stream)
                df.rename(columns=lambda x: x.strip(), inplace=True)
                if 'Post Date' in df.columns: df.rename(columns={'Post Date': 'Date'}, inplace=True)
                if 'Memo' in df.columns: df.rename(columns={'Memo': 'Description'}, inplace=True)
            elif file.filename.lower().endswith('.pdf'):
                data = parse_chase_pdf(file.stream)
                if data: df = pd.DataFrame(data)
            if df.empty: continue
            if not all(c in df.columns for c in ['Date','Amount','Description']):
                flash(f"File {file.filename} missing columns.", "danger")
                continue
            for _, row in df.iterrows():
                try:
                    dvals = row.get('Date')
                    date_val = pd.to_datetime(dvals).date() if isinstance(dvals, str) else dvals
                    desc = str(row.get('Description', '')).strip()
                    amount = float(str(row.get('Amount')).replace('$','').replace(',',''))
                    if account.account_type == 'credit_card' and amount > 0: amount = -amount
                    if Transaction.query.filter_by(account_id=account.id, date=date_val, amount=amount, original_description=desc).first():
                        skipped += 1
                        continue
                    payee = Payee.query.filter_by(name=desc.title()).first()
                    if not payee:
                        payee = Payee(name=desc.title())
                        db.session.add(payee)
                        db.session.commit()
                    cat_id = apply_rules_to_payee(payee, uncat_id)
                    db.session.add(Transaction(date=date_val, original_description=desc, amount=amount, payee_id=payee.id, category_id=cat_id, account_id=account.id))
                    added += 1
                except Exception as e: print(f"Row Error: {e}")
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"File Error {file.filename}: {e}", "danger")
    flash(f"Imported {added}. Skipped {skipped} duplicates.", "success")
    return redirect(url_for('index'))

@app.route('/manage_payees')
def manage_payees():
    search_query = request.args.get('search', '').strip()
    rules = PayeeRule.query.order_by(PayeeRule.display_name).all()
    query = db.session.query(Payee, PayeeRule.display_name, Category.name.label('category_name')).select_from(Payee).outerjoin(PayeeRule).outerjoin(Category)
    if search_query:
        term = f"%{search_query}%"
        query = query.filter(or_(Payee.name.ilike(term), PayeeRule.display_name.ilike(term)))
    payees = query.order_by(case((Payee.rule_id == None, 1), else_=0).desc(), Payee.name).all()
    return render_template('manage_payees.html', payees_data=payees, rules=rules, search_query=search_query)

@app.route('/manage_payees/add', methods=['POST'])
def add_payee():
    name = request.form.get('new_payee_name', '').strip().title()
    if name and not Payee.query.filter_by(name=name).first():
        db.session.add(Payee(name=name))
        db.session.commit()
        flash(f"Payee '{name}' added.", "success")
    return redirect(url_for('manage_payees'))

@app.route('/manage_payees/rename', methods=['POST'])
def rename_payee():
    payee = db.session.get(Payee, request.form.get('payee_id'))
    new = request.form.get('payee_name', '').strip()
    if payee and new and not Payee.query.filter(Payee.name==new, Payee.id!=payee.id).first():
        payee.name = new
        db.session.commit()
        flash("Renamed.", "success")
    return redirect(url_for('manage_payees'))

@app.route('/manage_payees/delete/<int:payee_id>', methods=['POST'])
def delete_payee(payee_id):
    if not Transaction.query.filter_by(payee_id=payee_id).count():
        Payee.query.filter_by(id=payee_id).delete()
        db.session.commit()
        flash("Deleted.", "success")
    else: flash("Cannot delete payee with transactions.", "danger")
    return redirect(url_for('manage_payees'))

@app.route('/manage_payees/link', methods=['POST'])
def link_payee():
    payee = db.session.get(Payee, request.form.get('payee_id'))
    if not payee: return jsonify({'success': False})
    rule_id = request.form.get('rule_id')
    new_rule_id, new_cat_id = None, get_uncategorized_id()
    if rule_id and rule_id != 'None':
        rule = db.session.get(PayeeRule, rule_id)
        if rule: new_rule_id, new_cat_id = rule.id, rule.category_id
    payee.rule_id = new_rule_id
    Transaction.query.filter_by(payee_id=payee.id).update({'category_id': new_cat_id}, synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/manage_rules', methods=['GET','POST'])
def manage_rules():
    if request.method == 'POST':
        rule_id = request.form.get('rule_id')
        frag = request.form.get('fragment', '').strip().upper()
        disp = request.form.get('display_name', '').strip()
        cat = request.form.get('category_id')
        if rule_id:
            r = db.session.get(PayeeRule, rule_id)
            if r: r.fragment, r.display_name, r.category_id = frag, disp, cat
            run_rule_on_all_payees(r, overwrite=False)
        elif not PayeeRule.query.filter_by(fragment=frag).first():
            new_rule = PayeeRule(fragment=frag, display_name=disp, category_id=cat)
            db.session.add(new_rule)
            db.session.commit()
            run_rule_on_all_payees(new_rule, overwrite=False)
        db.session.commit()
        return redirect(url_for('manage_rules'))
    search_query = request.args.get('search', '').strip()
    query = PayeeRule.query.join(Category)
    if search_query:
        term = f"%{search_query}%"
        query = query.filter(or_(PayeeRule.fragment.ilike(term), PayeeRule.display_name.ilike(term), Category.name.ilike(term)))
    rules = query.order_by(PayeeRule.display_name).all()
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped = {}
    for c in cats:
        if c.type not in grouped: grouped[c.type] = []
        grouped[c.type].append(c)
    return render_template('manage_payee_rules.html', rules=rules, grouped_categories=grouped, search_query=search_query)

@app.route('/manage_rules/delete/<int:rule_id>', methods=['POST'])
def delete_rule(rule_id):
    rule = db.session.get(PayeeRule, rule_id)
    if rule:
        Payee.query.filter_by(rule_id=rule.id).update({'rule_id': None})
        Transaction.query.filter(Transaction.payee_id.in_([p.id for p in rule.payees])).update({'category_id': get_uncategorized_id()}, synchronize_session=False)
        db.session.delete(rule)
        db.session.commit()
        flash("Rule deleted.", "success")
    return redirect(url_for('manage_rules'))

@app.route('/manage_rules/apply/<int:rule_id>', methods=['POST'])
def apply_rule_force(rule_id):
    rule = db.session.get(PayeeRule, rule_id)
    if rule:
        count = run_rule_on_all_payees(rule, overwrite=True)
        flash(f"Applied rule to {count} payees.", "success")
    else: flash("Rule not found.", "danger")
    return redirect(url_for('manage_rules'))

@app.route('/manage_rules/run_all', methods=['POST'])
def run_all_rules_force():
    rules = PayeeRule.query.order_by(PayeeRule.id.asc()).all()
    total = 0
    for r in rules: total += run_rule_on_all_payees(r, overwrite=True)
    flash(f"Ran {len(rules)} rules. Updated {total} payees.", "success")
    return redirect(url_for('manage_rules'))

@app.route('/categorize', methods=['GET','POST'])
def categorize():
    if request.method == 'POST':
        t = db.session.get(Transaction, request.form.get('transaction_id'))
        cat = int(request.form.get('category_id'))
        frag = request.form.get('rule_fragment', '').strip().upper()
        disp = request.form.get('payee_display_name', '').strip()
        c_filt = request.form.get('current_filter', 'all')
        c_srch = request.form.get('current_search', '')
        c_year = request.form.get('current_year', '')
        c_month = request.form.get('current_month', '')
        
        if t and frag and disp:
            try:
                rule = PayeeRule.query.filter_by(fragment=frag).first()
                if rule:
                    rule.display_name, rule.category_id = disp, cat
                    flash_msg = "Updated rule."
                else:
                    rule = PayeeRule(fragment=frag, display_name=disp, category_id=cat)
                    db.session.add(rule)
                    flash_msg = "Created rule."
                db.session.commit()
                t.payee.rule_id, t.category_id = rule.id, cat
                db.session.commit()
                count = run_rule_on_all_payees(rule, overwrite=True)
                flash(f"{flash_msg} Applied to {count + 1}.", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Error: {e}", "danger")
        return redirect(url_for('categorize', filter_type=c_filt, search=c_srch, year=c_year, month=c_month))

    filt = request.args.get('filter_type', 'all')
    srch = request.args.get('search', '').strip()
    year = request.args.get('year', '')
    month = request.args.get('month', '')
    
    q = Transaction.query.join(Payee).filter(Transaction.is_deleted == False)
    if not srch: q = q.filter(Payee.rule_id == None)
    if filt == 'positive': q = q.filter(Transaction.amount > 0)
    elif filt == 'negative': q = q.filter(Transaction.amount < 0)
    if year and year != 'all': q = q.filter(extract('year', Transaction.date) == int(year))
    if month and month != 'all': q = q.filter(extract('month', Transaction.date) == int(month))
    if srch: q = q.filter(or_(Payee.name.ilike(f"%{srch}%"), Transaction.original_description.ilike(f"%{srch}%")))
    
    txs = q.order_by(Transaction.date.desc()).limit(500 if (srch or year or month) else 50).all()
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped = {}
    for c in cats:
        if c.type not in grouped: grouped[c.type] = []
        grouped[c.type].append(c)
    labels = [r.display_name for r in db.session.query(PayeeRule.display_name).distinct().order_by(PayeeRule.display_name).all()]
    available_years = [int(y[0]) for y in db.session.query(extract('year', Transaction.date)).distinct().order_by(extract('year', Transaction.date).desc()).all()]
    
    return render_template('categorize.html', transactions=txs, grouped_categories=grouped, filter_type=filt, search_query=srch, selected_year=year, selected_month=month, available_years=available_years, existing_labels=labels)

@app.route('/edit_transactions', defaults={'month_offset': '0'}, methods=['GET','POST'])
@app.route('/edit_transactions/<string:month_offset>', methods=['GET','POST'])
def edit_transactions(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    
    # Re-use get_monthly_summary_direct since we don't need full cache here
    summary = get_monthly_summary_direct(month_offset)
    
    srch = request.args.get('search', '').strip()
    
    if srch:
        current_context = f"Search Results: '{srch}'"
        query = Transaction.query.join(Payee).filter(or_(Payee.name.ilike(f"%{srch}%"), Transaction.original_description.ilike(f"%{srch}%")))
    else:
        current_context = summary['current_month_name']
        query = Transaction.query.join(Payee).filter(Transaction.date >= summary['start_date'], Transaction.date <= summary['end_date'])
        
    txs = query.order_by(Transaction.date.desc()).all()
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped = {}
    for c in cats:
        if c.type not in grouped: grouped[c.type] = []
        grouped[c.type].append(c)
    return render_template('edit_transactions.html', transactions=txs, payees=Payee.query.order_by(Payee.name).all(), grouped_categories=grouped, accounts=Account.query.all(), month_offset=month_offset, current_month_name=current_context, search_query=srch)

@app.route('/update_transaction/<int:t_id>', methods=['POST'])
def update_transaction(t_id):
    t = db.session.get(Transaction, t_id)
    if t:
        name = request.form.get('payee_name_input').title()
        payee = Payee.query.filter_by(name=name).first()
        if not payee:
            payee = Payee(name=name)
            db.session.add(payee)
            db.session.commit()
        t.payee_id, t.category_id, t.account_id = payee.id, int(request.form.get('category_id')), int(request.form.get('account_id'))
        t.date, t.amount, t.is_deleted = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date(), float(request.form.get('amount')), 'is_deleted' in request.form
        db.session.commit()
    return redirect(url_for('edit_transactions', month_offset=request.form.get('month_offset'), search=request.form.get('search_query')))

@app.route('/edit_accounts')
def edit_accounts():
    accs = Account.query.order_by(Account.name).all()
    for a in accs:
        net = db.session.query(func.sum(Transaction.amount)).filter(Transaction.account_id==a.id, Transaction.is_deleted==False).scalar() or 0
        a.current_balance = float(a.starting_balance) + float(net)
    return render_template('edit_account_balances.html', accounts=accs, account_types=['checking','savings','credit_card'])

@app.route('/add_account', methods=['POST'])
def add_account():
    n, t = request.form.get('account_name'), request.form.get('account_type')
    if not Account.query.filter_by(name=n, account_type=t).first():
        db.session.add(Account(name=n, account_type=t))
        db.session.commit()
    else: flash("Account exists.", "danger")
    return redirect(url_for('edit_accounts'))

@app.route('/update_account', methods=['POST'])
def update_account():
    a = db.session.get(Account, request.form.get('account_id'))
    if a:
        a.name, a.account_type, a.starting_balance = request.form.get('name'), request.form.get('account_type'), float(request.form.get('starting_balance'))
        db.session.commit()
    return redirect(url_for('edit_accounts'))

@app.route('/delete_account/<int:acc_id>', methods=['POST'])
def delete_account(acc_id):
    Account.query.filter_by(id=acc_id).delete()
    db.session.commit()
    return redirect(url_for('edit_accounts'))

@app.route('/calculate_starting_balance/<int:account_id>', methods=['POST'])
def calculate_starting_balance(account_id):
    try:
        curr = float(request.get_json().get('current_actual_balance', '0').replace('$','').replace(',',''))
        net = db.session.query(func.sum(Transaction.amount)).filter(Transaction.account_id == account_id, Transaction.is_deleted == False).scalar() or 0
        return jsonify({'success': True, 'new_starting_balance': f"{curr - float(net):.2f}"})
    except Exception as e: return jsonify({'success': False, 'message': str(e)})

@app.route('/manage_categories', methods=['GET','POST'])
def manage_categories():
    if request.method == 'POST':
        n, t = request.form.get('name'), request.form.get('type')
        if not Category.query.filter_by(name=n).first():
            db.session.add(Category(name=n, type=t))
            db.session.commit()
        return redirect(url_for('manage_categories'))
    return render_template('manage_categories.html', categories=Category.query.order_by(Category.type, Category.name).all())

@app.route('/manage_categories/delete/<int:cat_id>', methods=['POST'])
def delete_category(cat_id):
    uncat = get_uncategorized_id()
    if cat_id != uncat:
        Transaction.query.filter_by(category_id=cat_id).update({'category_id': uncat})
        PayeeRule.query.filter_by(category_id=cat_id).update({'category_id': uncat})
        Category.query.filter_by(id=cat_id).delete()
        db.session.commit()
    return redirect(url_for('manage_categories'))

@app.route('/manage_categories/rename', methods=['POST'])
def rename_category():
    cat = db.session.get(Category, request.form.get('category_id'))
    new = request.form.get('category_name', '').strip()
    if cat and new and cat.name != 'Uncategorized' and not Category.query.filter(Category.name==new, Category.id!=cat.id).first():
        cat.name = new
        db.session.commit()
        flash(f"Renamed to {new}.", "success")
    else: flash("Error renaming category.", "danger")
    return redirect(url_for('manage_categories'))

@app.route('/events')
def events(): return render_template('events.html', events=Event.query.order_by(Event.date.desc()).all())

@app.route('/add_event', methods=['POST'])
def add_event():
    d, desc = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date(), request.form.get('description')
    if not Event.query.filter_by(date=d, description=desc).first():
        db.session.add(Event(date=d, description=desc))
        db.session.commit()
    else: flash("Event exists.", "danger")
    return redirect(url_for('events'))

@app.route('/delete_event/<int:eid>', methods=['POST'])
def delete_event(eid):
    Event.query.filter_by(id=eid).delete()
    db.session.commit()
    return redirect(url_for('events'))

@app.route('/trends')
def trends():
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped_categories = {}
    for c in cats:
        if c.type not in grouped_categories: grouped_categories[c.type] = []
        grouped_categories[c.type].append(c)
    results = db.session.query(func.coalesce(PayeeRule.display_name, Payee.name).label('label')).select_from(Payee).outerjoin(PayeeRule).distinct().order_by('label').all()
    unique_labels = [r.label for r in results if r.label]
    return render_template('trends.html', grouped_categories=grouped_categories, all_events=Event.query.all(), payee_labels=unique_labels)

@app.route('/api/trend_data', methods=['POST'])
def get_trend_data():
    data = request.get_json()
    cat_ids = data.get('category_ids', [])
    payee_names = data.get('payee_names', [])
    evt_ids = [int(i) for i in data.get('event_ids', [])]
    s_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
    e_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
    bucket = data.get('time_bucket', 'month')
    chart_type = 'scatter' if data.get('chart_type') in ['line','area'] else 'bar'
    chart_mode = 'lines+markers' if data.get('chart_type') == 'line' else None
    fill_type = 'tozeroy' if data.get('chart_type') == 'area' else None

    data_by_item, all_buckets = {}, set()
    
    if cat_ids:
        bucket_col = func.date_trunc(bucket, Transaction.date)
        query_cat = db.session.query(bucket_col.label('bucket_start'), Category.name.label('name'), func.sum(Transaction.amount).label('total')).select_from(Transaction).join(Category).filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False, Transaction.category_id.in_(cat_ids)).group_by(bucket_col, Category.name).order_by(bucket_col)
        for row in query_cat.all():
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            all_buckets.add(b_date)
            if row.name not in data_by_item: data_by_item[row.name] = {}
            data_by_item[row.name][b_date] = data_by_item[row.name].get(b_date, 0) + abs(float(row.total))

    if payee_names:
        bucket_col = func.date_trunc(bucket, Transaction.date)
        name_col = func.coalesce(PayeeRule.display_name, Payee.name)
        query_payee = db.session.query(bucket_col.label('bucket_start'), name_col.label('name'), func.sum(Transaction.amount).label('total')).select_from(Transaction).join(Payee).outerjoin(PayeeRule).filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False, name_col.in_(payee_names)).group_by(bucket_col, name_col).order_by(bucket_col)
        for row in query_payee.all():
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            all_buckets.add(b_date)
            name = f"[P] {row.name}"
            if name not in data_by_item: data_by_item[name] = {}
            data_by_item[name][b_date] = data_by_item[name].get(b_date, 0) + abs(float(row.total))

    sorted_buckets = sorted(list(all_buckets))
    plot_data = [{'type': chart_type, 'name': name, 'x': sorted_buckets, 'y': [pts.get(b, 0) for b in sorted_buckets], 'mode': chart_mode, 'fill': fill_type} for name, pts in data_by_item.items()]
    
    events = Event.query.filter(Event.date >= s_date, Event.date <= e_date, Event.id.in_(evt_ids)).all()
    shapes = [{'type': 'line', 'x0': e.date.strftime('%Y-%m-%d'), 'x1': e.date.strftime('%Y-%m-%d'), 'y0':0, 'y1':1, 'yref':'paper', 'line': {'color':'pink', 'dash':'dot'}} for e in events]
    anns = [{'x': e.date.strftime('%Y-%m-%d'), 'y':0.95, 'yref':'paper', 'text': e.description, 'showarrow':False, 'bgcolor':'rgba(255,192,203,0.8)'} for e in events]
    return jsonify({'plot_data': plot_data, 'layout_shapes': shapes, 'layout_annotations': anns})

@app.route('/api/min_max_dates')
def api_date_range():
    min_date = db.session.query(func.min(Transaction.date)).scalar()
    max_date = db.session.query(func.max(Transaction.date)).scalar()
    return jsonify({'min_date': min_date.strftime('%Y-%m-%d') if min_date else date.today().strftime('%Y-%m-%d'), 'max_date': max_date.strftime('%Y-%m-%d') if max_date else date.today().strftime('%Y-%m-%d')})

if __name__ == '__main__':
    with app.app_context(): create_tables()
    app.run(debug=True)