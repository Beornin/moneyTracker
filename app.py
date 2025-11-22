import os
import calendar
import re
import io
from datetime import datetime, date, timedelta 
import pdfplumber 
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, extract, case, or_, text
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
EXCLUDED_CAT = 'Ignored Credit Card Payment'
CHASE_DATE_REGEX = re.compile(r"Opening/Closing Date\s*([\d/]+)\s*-\s*([\d/]+)")
CHASE_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*)")
AMOUNT_REGEX = re.compile(r"([\d,]+\.\d{2})(?=\s*$)")

# --- MIXINS ---

class TimestampMixin(object):
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

# --- MODELS ---

class Account(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    account_type = db.Column(db.String(50), nullable=False) 
    transactions = db.relationship('Transaction', backref='account', lazy=True, cascade="all, delete-orphan")
    __table_args__ = (db.UniqueConstraint('name', 'account_type', name='_account_uc'),)

class Category(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    type = db.Column(db.String(20), nullable=False)
    transactions = db.relationship('Transaction', backref='category', lazy=True)
    payee_rules = db.relationship('PayeeRule', backref='category', lazy=True)
    __table_args__ = (db.Index('idx_category_name', 'name'),)

class PayeeRule(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    fragment = db.Column(db.String(500), nullable=False)
    display_name = db.Column(db.String(500), nullable=False)
    match_type = db.Column(db.String(20), default='any', nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    payees = db.relationship('Payee', backref='rule', lazy=True)
    
    __table_args__ = (
        db.Index('idx_rule_fragment', 'fragment'),
        db.UniqueConstraint('fragment', 'match_type', name='_rule_frag_type_uc'),
    )

class Payee(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(500), nullable=False, unique=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('payee_rule.id'), nullable=True)
    transactions = db.relationship('Transaction', backref='payee', lazy=True)
    __table_args__ = (db.Index('idx_payee_name', 'name'),)

class Transaction(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    original_description = db.Column(db.String(500), nullable=True) 
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payee_id = db.Column(db.Integer, db.ForeignKey('payee.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    
    __table_args__ = (
        db.Index('idx_tx_date_deleted', 'date', 'is_deleted'),
        db.Index('idx_tx_category', 'category_id'),
        db.Index('idx_tx_account', 'account_id'),
    )

class Event(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500), nullable=False)
    __table_args__ = (db.UniqueConstraint('date', 'description', name='_event_uc'),)

# --- INITIALIZATION ---

def create_tables():
    db.create_all()
    try:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE transaction DROP CONSTRAINT IF EXISTS _unique_tx_uc"))
            conn.commit()
    except Exception as e:
        print(f"Constraint cleanup skipped: {e}")

    if Category.query.count() == 0:
        uncat = Category(name='Uncategorized', type='Expense')
        db.session.add(uncat)
        db.session.commit() 
        initial_cats = [
            {'name': 'Salary', 'type': 'Income'}, {'name': 'Empower IRA', 'type': 'Income'}, {'name': 'Rental', 'type': 'Income'}, {'name': 'Venmo', 'type': 'Income'}, {'name': 'Checks', 'type': 'Income'}, {'name': 'Investment Income', 'type': 'Income'}, {'name': 'Reimbursements', 'type': 'Income'}, {'name': 'Other Income', 'type': 'Income'},
            {'name': 'Housing', 'type': 'Expense'}, {'name': 'Pets', 'type': 'Expense'}, {'name': 'Car Payment', 'type': 'Expense'}, {'name': 'Utilities', 'type': 'Expense'}, {'name': 'Groceries', 'type': 'Expense'}, {'name': 'Transportation', 'type': 'Expense'}, {'name': 'Insurance', 'type': 'Expense'}, {'name': 'Medical', 'type': 'Expense'}, {'name': 'Education', 'type': 'Expense'},
            {'name': 'Eat Out', 'type': 'Expense'}, {'name': 'Shopping', 'type': 'Expense'}, {'name': 'Entertainment', 'type': 'Expense'}, {'name': 'Personal Care', 'type': 'Expense'}, {'name': 'Travel', 'type': 'Expense'}, {'name': 'Household', 'type': 'Expense'}, {'name': 'Gifts & Donations', 'type': 'Expense'},
            {'name': 'Ignored Credit Card Payment', 'type': 'Transfer'}, {'name': 'Savings Transfer', 'type': 'Transfer'}, {'name': 'Investment Transfer', 'type': 'Transfer'},
        ]
        for c in initial_cats: db.session.add(Category(**c))
        db.session.commit()

# --- LOGIC HELPERS ---

def get_uncategorized_id():
    return Category.query.filter_by(name='Uncategorized').first().id

def apply_rules_to_payee(payee, uncat_id, amount):
    if payee.rule_id: 
        rule = payee.rule
        match = True
        if rule.match_type == 'positive' and amount <= 0: match = False
        if rule.match_type == 'negative' and amount >= 0: match = False
        if match: return rule.category_id

    rules = PayeeRule.query.all()
    name_upper = payee.name.upper()
    for r in rules:
        if r.fragment.upper() in name_upper:
            if r.match_type == 'negative' and amount > 0: continue
            if r.match_type == 'positive' and amount < 0: continue
            payee.rule_id = r.id
            db.session.commit()
            return r.category_id
    return uncat_id

def run_rule_on_all_payees(rule, overwrite=False):
    uncat_id = get_uncategorized_id()
    matches = Payee.query.filter(Payee.name.ilike(f"%{rule.fragment}%")).all()
    if not matches: return 0
    count = 0
    for p in matches:
        if overwrite or not p.rule_id:
            p.rule_id = rule.id
        for t in p.transactions:
            is_match = True
            if rule.match_type == 'positive' and t.amount <= 0: is_match = False
            if rule.match_type == 'negative' and t.amount >= 0: is_match = False
            if is_match:
                t.category_id = rule.category_id
                count += 1
    db.session.commit()
    return count

def parse_chase_pdf(file_stream):
    transactions = []
    try:
        with pdfplumber.open(file_stream) as pdf:
            page1_text = pdf.pages[0].extract_text()
            date_match = CHASE_DATE_REGEX.search(page1_text)
            if not date_match: return None
            end_date = datetime.strptime(date_match.group(2), '%m/%d/%y').date()
            end_year, end_month = end_date.year, end_date.month

            full_text = ""
            for p in pdf.pages:
                txt = p.extract_text()
                if txt: full_text += txt + "\n"
            lines = full_text.split('\n')
            current_multiplier = -1 
            for line in lines:
                if "PAYMENTS AND OTHER CREDITS" in line.upper(): current_multiplier = 1
                elif "PURCHASE" in line.upper(): current_multiplier = -1
                match = CHASE_LINE_REGEX.search(line)
                if match:
                    d_str, remainder = match.groups()
                    amt_match = AMOUNT_REGEX.search(remainder)
                    if amt_match:
                        amt_str = amt_match.group(1)
                        desc_raw = remainder[:amt_match.start()].strip()
                        if "Order Number" in desc_raw: continue
                        try:
                            t_month = int(d_str.split('/')[0])
                            year = end_year if t_month <= end_month else end_year - 1
                            dt = datetime.strptime(f"{t_month}/{d_str.split('/')[1]}/{year}", '%m/%d/%Y').date()
                            val = abs(float(amt_str.replace(',', '')))
                            final_amount = val * current_multiplier
                            transactions.append({'Date': dt, 'Description': desc_raw, 'Amount': final_amount})
                        except Exception: continue
        return transactions
    except Exception as e: print(f"PDF Parse Error: {e}"); return None

def get_monthly_summary_direct(month_offset):
    today = date.today()
    idx = today.month - 1 + month_offset
    year, month = today.year + (idx // 12), (idx % 12) + 1
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    current_month_name = start.strftime('%B %Y')
    return {'start_date': start, 'end_date': end, 'current_month_name': current_month_name}

# --- DASHBOARD SERVICE ---

class DashboardService:
    def __init__(self):
        self.today = date.today()
        self.start_date_24mo = date(self.today.year - 2, self.today.month, 1)
        self.transactions = Transaction.query.options(
            joinedload(Transaction.category),
            joinedload(Transaction.account),
            joinedload(Transaction.payee).joinedload(Payee.rule)
        ).filter(
            Transaction.date >= self.start_date_24mo,
            Transaction.is_deleted == False
        ).all()

        self.events = Event.query.filter(Event.date >= self.start_date_24mo).all()
        self.savings_account_ids = {a.id for a in Account.query.filter_by(account_type='savings').all()}
        
        self.base_layout = dict(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        self.margin_std = dict(t=40, b=20, l=50, r=10)
        self.margin_legend = dict(t=40, b=100, l=50, r=10) 
        self.margin_events = dict(t=60, b=20, l=50, r=10)
        self.xaxis_date = dict(rangeslider=dict(visible=True), type='date', tickformat='%b %Y')
        self.xaxis_cat = dict(rangeslider=dict(visible=False), type='category')

    def get_summary_for_dashboard(self, month_offset):
        idx = self.today.month - 1 + month_offset
        year, month = self.today.year + (idx // 12), (idx % 12) + 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        current_month_name = start.strftime('%B %Y')
        txs = [t for t in self.transactions if start <= t.date <= end]
        inc = sum(t.amount for t in txs if t.category.type == 'Income' and t.category.name != EXCLUDED_CAT)
        exp = abs(sum(t.amount for t in txs if t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT))
        s_in = sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount > 0 and (t.category.type in ['Transfer', 'Income']))
        s_out = abs(sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount < 0 and t.category.type == 'Transfer'))
        net_worth = 0.0
        balances = {}
        uncat = Transaction.query.join(Payee).filter(Payee.rule_id == None, Transaction.is_deleted == False).count()
        dup_count = 0 
        return {'start_date': start, 'end_date': end, 'current_month_name': current_month_name, 'total_income': float(inc), 'total_expense': float(exp), 'net_worth': net_worth, 'account_balances': balances, 'uncategorized_count': uncat, 'savings_in': float(s_in), 'savings_out': float(s_out), 'duplicate_count': dup_count}

    def _get_event_overlays(self, start, end):
        relevant_events = [e for e in self.events if start <= e.date <= end]
        events_map = {}
        for e in relevant_events:
            key = e.date.replace(day=1).strftime('%Y-%m-%d')
            if key not in events_map: events_map[key] = []
            events_map[key].append(e.description)
        shapes = [{'type': 'line', 'x0': k, 'x1': k, 'y0': 0, 'y1': 1, 'xref': 'x', 'yref': 'paper', 'line': {'color': '#9ca3af', 'width': 1.5, 'dash': 'dot'}} for k in events_map]
        anns = [{'x': k, 'y': 1.02, 'xref': 'x', 'yref': 'paper', 'text': "📍 " + "<br>📍 ".join(v), 'showarrow': False, 'xanchor': 'center', 'yanchor': 'bottom', 'font': {'size': 10, 'color': '#4b5563'}, 'align': 'center'} for k, v in events_map.items()] 
        return shapes, anns

    def generate_all_charts(self):
        y_start, m_start = self.today.year, self.today.month - 17
        while m_start <= 0: 
            m_start += 12
            y_start -= 1
        s18 = date(y_start, m_start, 1)
        e18 = date(self.today.year, self.today.month, calendar.monthrange(self.today.year, self.today.month)[1])
        txs_18 = [t for t in self.transactions if s18 <= t.date <= e18]
        shapes, anns = self._get_event_overlays(s18, e18)
        months = []
        curr = s18
        while curr <= e18:
            months.append(curr)
            if curr.month == 12: curr = date(curr.year + 1, 1, 1)
            else: curr = date(curr.year, curr.month + 1, 1)
        month_strs = [m.strftime('%Y-%m-%d') for m in months]
        return {
            'chart_income_vs_expense': self._chart_income_vs_expense(months, month_strs, txs_18, shapes, anns),
            'chart_savings': self._chart_savings(months, month_strs, txs_18, shapes, anns),
            'chart_cash_flow': self._chart_cash_flow(months, month_strs, txs_18, shapes, anns),
            'chart_core_operating': self._chart_core_operating(months, month_strs, txs_18, shapes, anns),
            'chart_groceries': self._chart_groceries(months, month_strs, txs_18, shapes, anns),
            'chart_expense_broad': self._chart_expense_broad(months, month_strs, txs_18, shapes, anns),
            'chart_core_summary': self._chart_core_summary(months, month_strs, txs_18, shapes, anns),
            'chart_top_payees': self._chart_top_payees(txs_18, s18), 
            'chart_yoy': self._chart_yoy()
        }

    def _chart_income_vs_expense(self, months, month_strs, txs, shapes, anns):
        incs, exps = [], []
        cumulative_savings = 0.0 
        cumulative_data = []
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.name != EXCLUDED_CAT]
            curr_inc = sum(t.amount for t in m_txs if t.category.type == 'Income')
            curr_exp = sum(t.amount for t in m_txs if t.category.type == 'Expense')
            incs.append(float(curr_inc))
            exps.append(float(abs(curr_exp)))
            monthly_net = float(curr_inc) - float(abs(curr_exp))
            cumulative_savings += monthly_net
            cumulative_data.append(cumulative_savings)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Income', x=month_strs, y=incs, marker_color='#22c55e'))
        fig.add_trace(go.Bar(name='Expense', x=month_strs, y=exps, marker_color='#ef4444'))
        fig.add_trace(go.Scatter(name='Cumulative Savings', x=month_strs, y=cumulative_data, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Income vs Expenses (Budget View)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=dict(t=60, b=100, l=50, r=10), **self.base_layout)
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
        inc, exp, net = [], [], []
        cumulative_flow = 0.0 
        cumulative_data = []
        valid_transfers = ['Investment Transfer', 'Savings Transfer']
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.name != EXCLUDED_CAT]
            i_val = sum(t.amount for t in m_txs if (t.category.type == 'Income') or (t.amount > 0 and t.category.name in valid_transfers) or (t.amount > 0 and t.category.type == 'Expense')) 
            e_val = sum(t.amount for t in m_txs if (t.category.type == 'Expense' and t.amount < 0) or (t.amount < 0 and t.category.name in valid_transfers))
            i_val = float(i_val)
            e_val = float(abs(e_val))
            monthly_net = i_val - e_val
            cumulative_flow += monthly_net
            inc.append(i_val); exp.append(e_val); net.append(monthly_net)
            cumulative_data.append(cumulative_flow)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Total Inflow', x=month_strs, y=inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Total Outflow', x=month_strs, y=exp, marker_color='#ef4444'))
        fig.add_trace(go.Scatter(name='Cumulative Net Flow', x=month_strs, y=cumulative_data, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Total Cash Flow (Liquidity View)', yaxis=dict(title='Flow Volume ($)', tickformat="$,.0f"), barmode='group', xaxis=self.xaxis_date, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=self.margin_legend, shapes=shapes, annotations=anns, **self.base_layout)
        return to_json(fig, pretty=True)
    
    def _chart_core_summary(self, months, month_strs, txs, shapes, anns):
        # GOAL: Top 20 Payees specifically from the "Core Expenses" bucket
        # This drills down into the Purple bars of the neighbor chart
        
        excl = ['Car Payment', 'VUL', 'AC Payment']
        payee_map = {}
        
        for m in months:
            # Filter for this month
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month]
            
            for t in m_txs:
                # Logic matches Core Operating Expense definition:
                # Type is Expense AND Category is NOT in the exclusion list
                if t.category.type == 'Expense' and t.category.name not in excl:
                    # Use Rule Display Name if available, else Raw Name
                    name = t.payee.rule.display_name if t.payee.rule else t.payee.name
                    
                    # Sum up (Expenses are negative, we sum them as is)
                    payee_map[name] = payee_map.get(name, 0.0) + float(t.amount)
        
        # Sort by magnitude (biggest spenders)
        # expenses are negative, so we want the "smallest" numbers (e.g. -5000 < -100)
        sorted_payees = sorted(payee_map.items(), key=lambda item: item[1])[:20]
        
        # Reverse for Plotly Horizontal Bar (Top item at top of chart)
        sorted_payees = sorted_payees[::-1] 
        
        names = [p[0] for p in sorted_payees]
        # Convert to positive for display
        vals = [abs(p[1]) for p in sorted_payees] 
        
        fig = go.Figure(data=[go.Bar(
            x=vals, 
            y=names, 
            orientation='h', 
            marker_color='#6366f1', # Matches Core Expense Purple
            text=vals, 
            texttemplate='$%{x:,.0f}', 
            hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>'
        )])
        
        fig.update_layout(
            title='Top 20 Core Operating Payees (18 Mo)', 
            xaxis=dict(title='Total Spent', tickformat="$,.0f"), 
            margin=self.margin_std, 
            **self.base_layout
        )
        return to_json(fig, pretty=True)
    
    def _chart_core_operating(self, months, month_strs, txs, shapes, anns):
        c_inc, c_exp, net = [], [], []
        cumulative_surplus = 0.0
        cumulative_net = []
        excl = ['Car Payment', 'VUL', 'AC Payment']
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month]
            inc_val = float(sum(t.amount for t in m_txs if t.category.type == 'Income' and t.category.name != 'Empower IRA'))
            exp_val = float(abs(sum(t.amount for t in m_txs if t.category.type == 'Expense' and t.category.name not in excl)))
            current_surplus = inc_val - exp_val
            cumulative_surplus += current_surplus 
            c_inc.append(inc_val)
            c_exp.append(exp_val)
            cumulative_net.append(cumulative_surplus) 

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Core Income', x=month_strs, y=c_inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Core Expenses', x=month_strs, y=c_exp, marker_color='#6366f1'))
        fig.add_trace(go.Scatter(name='Cumulative Surplus', x=month_strs, y=cumulative_net, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Core Operating Performance (Day-to-Day)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=self.margin_legend, shapes=shapes, annotations=anns, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_groceries(self, months, month_strs, txs, shapes, anns):
        groc, dine = [], []
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month]
            groc.append(float(abs(sum(t.amount for t in m_txs if t.category.name == 'Groceries'))))
            dine.append(float(abs(sum(t.amount for t in m_txs if t.category.name == 'Eat Out'))))
        fig = go.Figure([go.Bar(name='Groceries', x=month_strs, y=groc, marker_color='#10b981'), go.Bar(name='Eat Out', x=month_strs, y=dine, marker_color='#f59e0b')])
        fig.update_layout(title='Groceries vs. Eating Out (18 Mo)', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_expense_broad(self, months, month_strs, txs, shapes, anns):
        cat_data = {}
        all_cats = set()
        for m in months:
            m_txs = [t for t in txs if t.date.year == m.year and t.date.month == m.month and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT]
            m_key = m.strftime('%Y-%m-%d')
            if m_key not in cat_data: cat_data[m_key] = {}
            temp_cat_totals = {}
            for t in m_txs:
                c_name = t.category.name
                all_cats.add(c_name)
                temp_cat_totals[c_name] = temp_cat_totals.get(c_name, 0.0) + float(t.amount)
            for c, val in temp_cat_totals.items():
                cat_data[m_key][c] = abs(val) if val < 0 else 0.0
        fig = go.Figure()
        for cat in sorted(list(all_cats), reverse=True):
            y_vals = [cat_data.get(m, {}).get(cat, 0) for m in month_strs]
            fig.add_trace(go.Bar(name=cat, x=month_strs, y=y_vals))
        fig.update_layout(title='Expenses by Category (Net, 18 Mo)', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, legend=dict(traceorder='reversed'), **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_top_payees(self, txs, start_date):
        payee_totals = {}
        for t in txs:
            if t.date >= start_date and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT:
                name = t.payee.rule.display_name if t.payee.rule else t.payee.name
                payee_totals[name] = payee_totals.get(name, 0) + float(t.amount)
        sorted_payees = sorted(payee_totals.items(), key=lambda item: item[1])[:20]
        sorted_payees = sorted_payees[::-1] 
        names = [p[0] for p in sorted_payees]
        vals = [abs(p[1]) for p in sorted_payees]
        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#6366f1', text=vals, texttemplate='$%{x:,.0f}', hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>')])
        fig.update_layout(title='Top 20 Payees (Last 18 Mo)', xaxis=dict(title='Total Spent', tickformat="$,.0f"), margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_yoy(self):
        curr_yr, last_yr = self.today.year, self.today.year - 1
        def get_yr_data(yr):
            monthly = [0.0] * 13
            yr_txs = [t for t in self.transactions if t.date.year == yr and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT]
            monthly_totals = {}
            for t in yr_txs:
                m = t.date.month
                monthly_totals[m] = monthly_totals.get(m, 0.0) + float(t.amount)
            for m, val in monthly_totals.items():
                monthly[m] = abs(val) if val < 0 else 0.0
            return monthly[1:]
        curr_data = get_yr_data(curr_yr)
        last_data = get_yr_data(last_yr)
        curr_data = [d if i < self.today.month else None for i, d in enumerate(curr_data)]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=list(calendar.month_name[1:]), y=last_data, mode='lines+markers', name=f'{last_yr}', line=dict(color='gray', dash='dash')))
        fig.add_trace(go.Scatter(x=list(calendar.month_name[1:]), y=curr_data, mode='lines+markers', name=f'{curr_yr}', line=dict(color='#6366f1', width=3)))
        fig.update_layout(title='YoY Expenses (Net)', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_cat, margin=self.margin_std, **self.base_layout)
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
    
    service = DashboardService()
    summary = service.get_summary_for_dashboard(month_offset)
    charts = service.generate_all_charts()
    
    return render_template('index.html', month_offset=month_offset, summary=summary, accounts=Account.query.all(), gemini_api_key=os.getenv('GEMINI_API_KEY'), **charts)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    account_id = request.form.get('account_id')
    files = request.files.getlist('file')
    if not account_id or not files: return redirect(url_for('index'))
    account = db.session.get(Account, account_id)
    if not account: abort(404)
    uncat_id = get_uncategorized_id()
    added, skipped, duplicates = 0, 0, 0
    
    for file in files:
        try:
            df = pd.DataFrame()
            if file.filename.lower().endswith('.csv'):
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                # CSV Format: Date, Amount, x, x, Description (Indices 0, 1, 4)
                df = pd.read_csv(stream)
                if df.shape[1] >= 5:
                    df = df.iloc[:, [0, 1, 4]]
                    df.columns = ['Date', 'Amount', 'Description']
                else:
                    flash(f"Skipping {file.filename}: Incorrect columns.", "warning")
                    continue
            elif file.filename.lower().endswith('.pdf'):
                data = parse_chase_pdf(file.stream)
                if data: df = pd.DataFrame(data)
            if df.empty: continue

            new_transactions = []
            
            for _, row in df.iterrows():
                try:
                    dvals = row.get('Date')
                    date_val = pd.to_datetime(dvals).date() if isinstance(dvals, str) else dvals
                    desc = str(row.get('Description', '')).strip()
                    amount = float(str(row.get('Amount')).replace('$','').replace(',',''))
                    if account.account_type == 'credit_card' and file.filename.lower().endswith('.csv') and amount > 0:
                        amount = -amount 
                    
                    payee = Payee.query.filter_by(name=desc.title()).first()
                    if not payee:
                        payee = Payee(name=desc.title())
                        db.session.add(payee)
                        db.session.commit()
                    
                    cat_id = apply_rules_to_payee(payee, uncat_id, amount)
                    
                    new_transactions.append(
                        Transaction(date=date_val, original_description=desc, amount=amount, payee_id=payee.id, category_id=cat_id, account_id=account.id)
                    )
                    added += 1
                except Exception as e: print(f"Row Error: {e}")
            
            if new_transactions:
                db.session.add_all(new_transactions)
                db.session.commit()
                
        except Exception as e:
            db.session.rollback()
            flash(f"File Error {file.filename}: {e}", "danger")
            
    flash(f"Imported {added} transactions.", "success")
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
    
    page = request.args.get('page', 1, type=int)
    per_page = 100
    # Pagination logic (manual slicing because query structure is complex)
    total = len(payees)
    start = (page - 1) * per_page
    end = start + per_page
    payees_paginated = payees[start:end]
    has_next = end < total
    has_prev = start > 0
    
    return render_template('manage_payees.html', payees_data=payees_paginated, rules=rules, search_query=search_query, page=page, has_next=has_next, has_prev=has_prev)

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
    return jsonify({'success': True, 'new_category_name': Category.query.get(new_cat_id).name})

@app.route('/manage_rules', methods=['GET','POST'])
def manage_rules():
    if request.method == 'POST':
        rule_id = request.form.get('rule_id')
        frag = request.form.get('fragment', '').strip().upper()
        disp = request.form.get('display_name', '').strip()
        cat = request.form.get('category_id')
        m_type = request.form.get('match_type', 'any')
        
        if rule_id:
            r = db.session.get(PayeeRule, rule_id)
            if r: 
                r.fragment, r.display_name, r.category_id, r.match_type = frag, disp, cat, m_type
                run_rule_on_all_payees(r, overwrite=False)
        elif not PayeeRule.query.filter_by(fragment=frag, match_type=m_type).first():
            new_rule = PayeeRule(fragment=frag, display_name=disp, category_id=cat, match_type=m_type)
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
        flash(f"Applied rule to {count} payees/transactions.", "success")
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
        transaction_id = request.form.get('transaction_id')
        category_id_str = request.form.get('category_id')
        save_one = request.form.get('save_one') == 'true'
        frag = request.form.get('rule_fragment', '').strip().upper()
        disp = request.form.get('payee_display_name', '').strip()
        c_filt = request.form.get('current_filter', 'all')
        c_srch = request.form.get('current_search', '')
        c_year = request.form.get('current_year', '')
        c_month = request.form.get('current_month', '')
        
        if transaction_id and category_id_str:
            try:
                t = db.session.get(Transaction, transaction_id)
                cat_id = int(category_id_str) 
                if t:
                    if save_one:
                        t.category_id = cat_id
                        db.session.commit()
                        flash("Updated single transaction.", "success")
                    elif frag and disp:
                        m_type = 'any'
                        if t.amount < 0: m_type = 'negative'
                        elif t.amount > 0: m_type = 'positive'

                        rule = PayeeRule.query.filter_by(fragment=frag, match_type=m_type).first()
                        
                        if rule:
                            rule.display_name = disp
                            rule.category_id = cat_id
                            flash_msg = f"Updated rule ({m_type})."
                        else:
                            rule = PayeeRule(fragment=frag, display_name=disp, category_id=cat_id, match_type=m_type)
                            db.session.add(rule)
                            flash_msg = f"Created rule ({m_type})."
                        
                        db.session.commit()
                        t.payee.rule_id = rule.id
                        t.category_id = cat_id
                        db.session.commit()
                        count = run_rule_on_all_payees(rule, overwrite=True)
                        flash(f"{flash_msg} Applied to {count + 1} transactions.", "success")
            except ValueError: flash("Invalid Category ID.", "danger")
            except Exception as e: db.session.rollback(); flash(f"Error: {e}", "danger")
        else: flash("Missing required fields.", "danger")
        return redirect(url_for('categorize', filter_type=c_filt, search=c_srch, year=c_year, month=c_month))

    filt = request.args.get('filter_type', 'all')
    srch = request.args.get('search', '').strip()
    year = request.args.get('year', '')
    month = request.args.get('month', '')
    uncat_category = Category.query.filter_by(name='Uncategorized').first()
    q = Transaction.query.join(Payee).filter(Transaction.is_deleted == False)
    
    if not srch and uncat_category: 
        q = q.filter(Transaction.category_id == uncat_category.id)
        
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
    month_choices = [(str(i), calendar.month_name[i]) for i in range(1, 13)]
    return render_template('categorize.html', transactions=txs, grouped_categories=grouped, filter_type=filt, search_query=srch, selected_year=year, selected_month=month, available_years=available_years, existing_labels=labels, month_choices=month_choices)

@app.route('/edit_transactions', defaults={'month_offset': '0'}, methods=['GET','POST'])
@app.route('/edit_transactions/<string:month_offset>', methods=['GET','POST'])
def edit_transactions(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    summary = get_monthly_summary_direct(month_offset)
    srch = request.args.get('search', '').strip()
    if srch:
        current_context = f"Search Results: '{srch}'"
        query = Transaction.query.join(Payee).filter(or_(Payee.name.ilike(f"%{srch}%"), Transaction.original_description.ilike(f"%{srch}%")))
    else:
        req_year = request.args.get('year')
        req_month = request.args.get('month')
        if req_year and req_month:
             query = Transaction.query.join(Payee).filter(extract('year', Transaction.date) == int(req_year), extract('month', Transaction.date) == int(req_month))
             current_context = f"Filter: {calendar.month_name[int(req_month)]} {req_year}"
        elif req_year:
             query = Transaction.query.join(Payee).filter(extract('year', Transaction.date) == int(req_year))
             current_context = f"Filter: {req_year}"
        else:
             current_context = summary['current_month_name']
             query = Transaction.query.join(Payee).filter(Transaction.date >= summary['start_date'], Transaction.date <= summary['end_date'])

    sort_by = request.args.get('sort_by', 'date')
    sort_order = request.args.get('sort_order', 'desc')

    if sort_by == 'payee': sort_attr = Payee.name
    elif sort_by == 'amount': sort_attr = Transaction.amount
    elif sort_by == 'category': sort_attr = Category.name; query = query.join(Category)
    elif sort_by == 'account': sort_attr = Account.name; query = query.join(Account)
    else: sort_attr = Transaction.date

    if sort_order == 'asc': query = query.order_by(sort_attr.asc())
    else: query = query.order_by(sort_attr.desc())

    if sort_by != 'date': query = query.order_by(Transaction.date.desc())

    txs = query.all()
    available_years = [int(y[0]) for y in db.session.query(extract('year', Transaction.date)).distinct().order_by(extract('year', Transaction.date).desc()).all()]
    month_choices = [(str(i), calendar.month_name[i]) for i in range(1, 13)]
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped = {}
    for c in cats:
        if c.type not in grouped: grouped[c.type] = []
        grouped[c.type].append(c)
        
    return render_template('edit_transactions.html', transactions=txs, payees=Payee.query.order_by(Payee.name).all(), grouped_categories=grouped, accounts=Account.query.all(), month_offset=month_offset, current_month_name=current_context, search_query=srch, available_years=available_years, month_choices=month_choices, selected_year=request.args.get('year'), selected_month=request.args.get('month'), sort_by=sort_by, sort_order=sort_order)

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
        a.name, a.account_type = request.form.get('name'), request.form.get('account_type') 
        db.session.commit()
    return redirect(url_for('edit_accounts'))

@app.route('/delete_account/<int:acc_id>', methods=['POST'])
def delete_account(acc_id):
    Account.query.filter_by(id=acc_id).delete()
    db.session.commit()
    return redirect(url_for('edit_accounts'))

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