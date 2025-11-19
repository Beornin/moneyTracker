import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, extract, case, or_
import plotly.graph_objects as go
from plotly.io import to_json 
import io
import calendar
import re
from datetime import datetime, date, timedelta 
import pdfplumber 
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# Update this connection string if using a different database
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://budget_user:ben@localhost:5432/budget_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24)

db = SQLAlchemy(app)

# --- MODELS ---

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    account_type = db.Column(db.String(50), nullable=False) 
    starting_balance = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)
    transactions = db.relationship('Transaction', backref='account', lazy=True, cascade="all, delete-orphan")

    # NEW: Prevent duplicate accounts with same name and type
    __table_args__ = (
        db.UniqueConstraint('name', 'account_type', name='_account_uc'),
    )

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    type = db.Column(db.String(20), nullable=False) # 'Income', 'Expense', 'Transfer'
    transactions = db.relationship('Transaction', backref='category', lazy=True)
    payee_rules = db.relationship('PayeeRule', backref='category', lazy=True)

class PayeeRule(db.Model):
    """Master rules for auto-categorization."""
    id = db.Column(db.Integer, primary_key=True)
    fragment = db.Column(db.String(500), nullable=False, unique=True)
    display_name = db.Column(db.String(500), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    payees = db.relationship('Payee', backref='rule', lazy=True)

class Payee(db.Model):
    """The unique, imported entity."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(500), nullable=False, unique=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('payee_rule.id'), nullable=True)
    transactions = db.relationship('Transaction', backref='payee', lazy=True)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    original_description = db.Column(db.String(500), nullable=True) 
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payee_id = db.Column(db.Integer, db.ForeignKey('payee.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    # NEW: Database-level constraint to prevent duplicate imports
    __table_args__ = (
        db.UniqueConstraint('date', 'original_description', 'amount', 'account_id', name='_unique_tx_uc'),
    )

class Event(db.Model):
    """Stores significant dates."""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500), nullable=False)

    # NEW: Prevent duplicate events with same date and name
    __table_args__ = (
        db.UniqueConstraint('date', 'description', name='_event_uc'),
    )

# --- INITIALIZATION ---

def create_tables():
    """Initializes schema and seeds default categories/payees if empty."""
    db.create_all()
    
    if Category.query.count() == 0:
        # 1. Create 'Uncategorized' first (Safety Net)
        uncat = Category(name='Uncategorized', type='Expense')
        db.session.add(uncat)
        db.session.commit() 
        
        # 2. Seed Standard Categories
        initial_cats = [
            {'name': 'Work', 'type': 'Income'},
            {'name': 'Empower', 'type': 'Income'},
            {'name': 'Checks', 'type': 'Income'},
            {'name': 'Rental', 'type': 'Income'},
            {'name': 'Venmo', 'type': 'Income'},
            {'name': 'Reimbursements', 'type': 'Income'},
            {'name': 'Other Income', 'type': 'Income'},

            {'name': 'Rental', 'type': 'Expense'},
            {'name': 'Taxes', 'type': 'Expense'},
            {'name': 'School', 'type': 'Expense'},
            {'name': 'Insurance', 'type': 'Expense'},
            {'name': 'Groceries', 'type': 'Expense'},
            {'name': 'Car Payment', 'type': 'Expense'},
            {'name': 'Tithe', 'type': 'Expense'},
            {'name': 'Eat Out', 'type': 'Expense'},
            {'name': 'Housing', 'type': 'Expense'},
            {'name': 'Gas', 'type': 'Expense'},
            {'name': 'Pets', 'type': 'Expense'},
            {'name': 'Utilities', 'type': 'Expense'},
            {'name': 'Vehicle Maint', 'type': 'Expense'},
            {'name': 'Medical', 'type': 'Expense'},
            {'name': 'Shopping', 'type': 'Expense'},
            {'name': 'Entertainment', 'type': 'Expense'},
            #this is here so we can ignore the cc payments in reports as both checking and cc statements have this line item
            {'name': 'Transfer Credit Card Payment', 'type': 'Transfer'},
            {'name': 'Transfer Fidelity', 'type': 'Transfer'},
            {'name': 'Transfer 529', 'type': 'Transfer'},
            {'name': 'Transfer Prudential Invest', 'type': 'Transfer'},
            {'name': 'Transfer Savings', 'type': 'Transfer'},
        ]
        for c in initial_cats:
            db.session.add(Category(**c))
        
        db.session.commit()

# --- RULE-BASED LOGIC HELPERS ---

def get_uncategorized_id():
    """Helper to get the 'Uncategorized' category ID."""
    uncat = Category.query.filter_by(name='Uncategorized').first()
    if not uncat:
        # This should not happen if create_tables() ran, but as a fallback
        uncat = Category(name='Uncategorized', type='Expense')
        db.session.add(uncat)
        db.session.commit()
    return uncat.id

def apply_rules_to_payee(payee, uncat_id):
    """
    Checks a Payee against all PayeeRules.
    Returns the category_id to assign.
    """
    if payee.rule_id:
        # This payee is already linked to a rule, use that rule's category
        return payee.rule.category_id

    # Payee is not linked, try to find a matching rule
    rules = PayeeRule.query.all()
    payee_name_upper = payee.name.upper()
    
    for rule in rules:
        if rule.fragment in payee_name_upper:
            # Found a match! Link the payee and return the category.
            payee.rule_id = rule.id
            db.session.commit()
            return rule.category_id
            
    # No rule matched
    return uncat_id

# We keep the original name so existing calls don't break
def run_rule_on_all_payees(rule, overwrite=False):
    """
    Runs a rule against payees. 
    If overwrite=False (default), only checks unlinked payees.
    If overwrite=True, checks ALL payees and updates them if they match.
    """
    uncat_id = get_uncategorized_id()
    
    if overwrite:
        # Fetch ALL payees to check for potential matches
        candidates = Payee.query.all()
    else:
        # Fetch only unlinked payees
        candidates = Payee.query.filter_by(rule_id=None).all()

    if not candidates: return 0

    # Find matches (Case-insensitive)
    matches = [p.id for p in candidates if rule.fragment in p.name.upper()]
    
    if not matches: return 0

    # Bulk Update Payees to link them to the Rule
    Payee.query.filter(Payee.id.in_(matches)).update({'rule_id': rule.id}, synchronize_session=False)
    
    # Bulk Update Transactions to the Rule's Category
    # If we are forcing an overwrite, we update the transaction category too.
    Transaction.query.filter(
        Transaction.payee_id.in_(matches)
    ).update({'category_id': rule.category_id}, synchronize_session=False)
    
    db.session.commit()
    return len(matches)


# --- PDF PARSING ---

def parse_chase_pdf(file_stream):
    """Parses a Chase credit card PDF statement."""
    transactions = []
    end_year, end_month = None, None
    date_regex = re.compile(r"Opening/Closing Date\s*([\d/]+)\s*-\s*([\d/]+)")
    trans_regex = re.compile(r"(\d{1,2}/\d{1,2})\s+(.+)\s+([\d,.-]+\.\d{2})")

    try:
        with pdfplumber.open(file_stream) as pdf:
            page1_text = pdf.pages[0].extract_text()
            date_match = date_regex.search(page1_text)
            if not date_match: return None
            
            end_date_str = date_match.group(2)
            end_date = datetime.strptime(end_date_str, '%m/%d/%y').date()
            end_year = end_date.year
            end_month = end_date.month

            full_text = ""
            for page in pdf.pages: full_text += page.extract_text() + "\n"

            matches = trans_regex.findall(full_text)
            for match in matches:
                trans_date_str, description, amount_str = match
                description = re.sub(r'\s+', ' ', description).strip()
                if "Order Number" in description: continue
                
                trans_month = int(trans_date_str.split('/')[0])
                year = end_year
                if trans_month > end_month: year = end_year - 1
                    
                full_date_str = f"{trans_month}/{trans_date_str.split('/')[1]}/{year}"
                trans_date = datetime.strptime(full_date_str, '%m/%d/%Y').date()
                
                try: amount = float(amount_str.replace(',', ''))
                except ValueError: continue 

                transactions.append({'Date': trans_date, 'Description': description, 'Amount': amount})
        return transactions
    except Exception as e:
        print(f"PDF Error: {e}")
        return None

# --- DASHBOARD HELPERS ---

def get_monthly_summary(month_offset):
    today = date.today()
    target_month_index = today.month - 1 + month_offset
    year = today.year + (target_month_index // 12)
    month = (target_month_index % 12) + 1
    
    start_date = date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    end_date = date(year, month, last_day)
    
    # Fetch transactions
    transactions = Transaction.query.join(Category).join(Payee).filter(
        Transaction.date >= start_date, 
        Transaction.date <= end_date, 
        Transaction.is_deleted == False
    ).order_by(Transaction.date.desc()).all()
    
    total_income = sum(t.amount for t in transactions if t.category.type == 'Income')
    net_expense_sum = sum(t.amount for t in transactions if t.category.type == 'Expense')
    total_expense = abs(net_expense_sum)
    
    category_spending = db.session.query(Category.name, func.sum(Transaction.amount).label('total')).join(Transaction).filter(
        Transaction.date >= start_date, 
        Transaction.date <= end_date, 
        Category.type == 'Expense', 
        Transaction.is_deleted == False
    ).group_by(Category.name).all()
    
    # --- SMART SAVINGS LOGIC ---
    savings_acc_ids = [a.id for a in Account.query.filter_by(account_type='savings').all()]
    savings_in = 0.0
    savings_out = 0.0
    
    if savings_acc_ids:
        # Get all transactions happening INSIDE savings accounts
        s_txs = Transaction.query.join(Category).filter(
            Transaction.date >= start_date, 
            Transaction.date <= end_date, 
            Transaction.account_id.in_(savings_acc_ids), 
            Transaction.is_deleted == False
        ).all()

        # 1. DEPOSITS: Include Transfers (from Checking) AND Income (Interest/IRA)
        # We want to know about ALL money added to savings
        savings_in = sum(t.amount for t in s_txs if t.amount > 0 and (t.category.type == 'Transfer' or t.category.type == 'Income'))
        
        # 2. WITHDRAWALS: Include Transfers ONLY.
        # We IGNORE Expenses (Type='Expense'). 
        # If you pay a Car Note (Expense) from Savings, it lowers the balance, 
        # but it won't show up on the "Savings Activity" chart as a "Withdrawal of Savings", 
        # keeping that chart focused purely on your savings habits.
        savings_out = abs(sum(t.amount for t in s_txs if t.amount < 0 and t.category.type == 'Transfer'))

    net_worth = 0.0
    account_balances = {}
    for acc in Account.query.all():
        net_change = db.session.query(func.sum(Transaction.amount)).filter(Transaction.account_id == acc.id, Transaction.is_deleted == False).scalar() or 0.0
        bal = float(acc.starting_balance) + float(net_change)
        account_balances[acc.name] = bal
        net_worth += bal

    uncat_count = Transaction.query.join(Payee).filter(Payee.rule_id == None, Transaction.is_deleted == False).count()

    return {
        'start_date': start_date, 'end_date': end_date,
        'current_month_name': start_date.strftime('%B %Y'),
        'total_income': total_income, 'total_expense': total_expense,
        'category_spending': category_spending, 'net_worth': net_worth,
        'account_balances': account_balances, 'uncategorized_count': uncat_count,
        'savings_in': float(savings_in),
        'savings_out': abs(float(savings_out))
    }

# --- PLOTTING ---

def create_plot(plot_type, data):
    # Standard Settings (Default Margins)
    layout_settings = dict(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#71717a'), margin={'t': 40, 'b': 20, 'l': 50, 'r': 10}, autosize=True
    )
    today = date.today()

    # CONFIG 1: For Time-Series Charts (Income, Savings, etc.)
    # Uses real dates so the slider works perfectly
    xaxis_date_config = dict(
        rangeslider=dict(visible=True),
        type='date',
        tickformat='%b %Y'  # Formats "2025-11-01" as "Nov 2025"
    )

    # CONFIG 2: For Category Charts (YoY, Top Payees)
    # Uses simple text labels (Jan, Feb, March)
    xaxis_cat_config = dict(
        rangeslider=dict(visible=False),
        type='category'
    )

    # --- 0. PREPARE EVENTS ---
    y_start, m_start = today.year, today.month - 11
    while m_start <= 0: m_start += 12; y_start -= 1
    graph_start_date = date(y_start, m_start, 1)
    
    _, last_day = calendar.monthrange(today.year, today.month)
    graph_end_date = date(today.year, today.month, last_day)

    events = Event.query.filter(Event.date >= graph_start_date, Event.date <= graph_end_date).all()
    
    events_map = {}
    for e in events:
        # Map event to the 1st of the month for alignment
        # Use ISO format "YYYY-MM-01" to match our new bar chart data
        cat_key = e.date.replace(day=1).strftime('%Y-%m-%d')
        if cat_key not in events_map: events_map[cat_key] = []
        events_map[cat_key].append(e.description)

    event_shapes = []
    event_annotations = []
    
    for cat_key, descriptions in events_map.items():
        combined_label = "📍 " + "<br>📍 ".join(descriptions)
        event_shapes.append({
            'type': 'line', 'x0': cat_key, 'x1': cat_key, 'y0': 0, 'y1': 1,
            'xref': 'x', 'yref': 'paper', 'line': {'color': '#9ca3af', 'width': 1.5, 'dash': 'dot'}
        })
        event_annotations.append({
            'x': cat_key, 'y': 1.02, 'xref': 'x', 'yref': 'paper',
            'text': combined_label, 'showarrow': False, 'xanchor': 'center', 'yanchor': 'bottom',
            'font': {'size': 10, 'color': '#4b5563'}, 'align': 'center'
        })

    # HELPER: Exclusion Check
    def is_valid(t):
        return t.category.name != 'Transfer Credit Card Payment'
    
    excluded_cat = 'Transfer Credit Card Payment'

    # 1. GROCERIES VS DINING
    if plot_type == 'groceries_vs_dining':
        months, groc, dine = [], [], []
        cat_groc = 'Groceries'
        cat_dine = 'Eat Out' 

        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0: m += 12; y -= 1
            while m > 12: m -= 12; y += 1
            s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            
            txs = Transaction.query.join(Category).filter(
                Transaction.date >= s, Transaction.date <= e, 
                Transaction.is_deleted == False,
                Category.name.in_([cat_groc, cat_dine])
            ).all()
            
            # FIX: Use ISO Date
            months.append(s.strftime('%Y-%m-%d'))
            groc.append(float(abs(sum(t.amount for t in txs if t.category.name == cat_groc)) or 0))
            dine.append(float(abs(sum(t.amount for t in txs if t.category.name == cat_dine)) or 0))
            
        fig = go.Figure(data=[
            go.Bar(name='Groceries', x=months, y=groc, marker_color='#10b981'),
            go.Bar(name='Eat Out', x=months, y=dine, marker_color='#f59e0b')
        ])
        fig.update_layout(
            title='Groceries vs. Eating Out (12 Mo)', barmode='group', 
            yaxis=dict(title='$', tickformat="$,.0f"), xaxis=xaxis_date_config, 
            shapes=event_shapes, annotations=event_annotations,
            margin=dict(t=60, b=20, l=50, r=10),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True
        )
        return to_json(fig, pretty=True)

    # 2. PAYEE BREAKDOWN (Weekly Stacked)
    elif plot_type == 'payee_pie':
        target_cats = ['Groceries', 'Eat Out']
        y_start, m_start = today.year, today.month - 11
        while m_start <= 0: m_start += 12; y_start -= 1
        start_date = date(y_start, m_start, 1)
        end_date = today

        display_name_expr = func.coalesce(PayeeRule.display_name, Payee.name)
        
        raw_data = db.session.query(
            func.date_trunc('week', Transaction.date).label('w'),
            display_name_expr.label('payee'),
            func.sum(Transaction.amount).label('total')
        ).select_from(Transaction)\
         .join(Payee)\
         .outerjoin(PayeeRule)\
         .join(Category)\
         .filter(
            Transaction.date >= start_date, 
            Transaction.date <= end_date,
            Category.name.in_(target_cats),
            Transaction.is_deleted == False
        ).group_by('w', display_name_expr).all()

        all_payees = set()
        data_map = {} 
        
        for r in raw_data:
            w_str = r.w.strftime('%Y-%m-%d')
            p_name = r.payee
            amt = abs(float(r.total or 0))
            all_payees.add(p_name)
            if w_str not in data_map: data_map[w_str] = {}
            data_map[w_str][p_name] = data_map[w_str].get(p_name, 0) + amt

        final_weeks = []
        current_w = start_date - timedelta(days=start_date.weekday())
        while current_w <= end_date:
            final_weeks.append(current_w.strftime('%Y-%m-%d'))
            current_w += timedelta(weeks=1)

        fig = go.Figure()
        for payee in sorted(list(all_payees)):
            y_values = []
            for w in final_weeks:
                y_values.append(data_map.get(w, {}).get(payee, 0))
            
            if sum(y_values) > 0:
                fig.add_trace(go.Bar(name=payee, x=final_weeks, y=y_values, text=y_values, texttemplate='%{y:,.0f}', textposition='auto'))

        fig.update_layout(
            title='Weekly Food Spending by Vendor (Stacked)',
            barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=xaxis_date_config,
            legend=dict(orientation="h", yanchor="bottom", y=-0.6, xanchor="center", x=0.5),
            margin=dict(t=40, b=100, l=50, r=10),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True
        )
        return to_json(fig, pretty=True)

    # 3. CORE OPERATING PERFORMANCE
    elif plot_type == 'core_operating':
        months, core_inc, core_exp, net_margin = [], [], [], []
        excluded_expenses = ['Car Payment', 'Insurance']

        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0: m += 12; y -= 1
            while m > 12: m -= 12; y += 1
            s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            
            txs = Transaction.query.join(Category).filter(Transaction.date >= s, Transaction.date <= e, Transaction.is_deleted == False).all()
            
            # FIX: Use ISO Date
            months.append(s.strftime('%Y-%m-%d'))
            
            inc_val = float(sum(t.amount for t in txs if t.category.type == 'Income') or 0)
            exp_val = 0.0
            for t in txs:
                if t.category.type == 'Expense' and t.category.name not in excluded_expenses:
                    exp_val += abs(float(t.amount))
            
            core_inc.append(inc_val)
            core_exp.append(exp_val)
            net_margin.append(inc_val - exp_val)
            
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Core Income', x=months, y=core_inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Core Expenses', x=months, y=core_exp, marker_color='#6366f1'))
        fig.add_trace(go.Scatter(name='Operating Surplus', x=months, y=net_margin, mode='lines+markers', line=dict(color='#f59e0b', width=3)))

        fig.update_layout(
            title='Core Operating Performance (Excl. Car & Insurance)', barmode='group', 
            yaxis=dict(title='$', tickformat="$,.0f"), xaxis=xaxis_date_config,
            showlegend=False,
            margin=dict(t=60, b=20, l=50, r=10),
            shapes=event_shapes, annotations=event_annotations,
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True
        )
        return to_json(fig, pretty=True)

    # 4. INCOME vs EXPENSE
    elif plot_type == 'income_vs_expense':
        months, incs, exps = [], [], []
        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0: m += 12; y -= 1
            while m > 12: m -= 12; y += 1
            s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            
            txs = Transaction.query.join(Category).filter(Transaction.date >= s, Transaction.date <= e, Transaction.is_deleted == False, Category.name != excluded_cat).all()
            
            # FIX: Use ISO Date
            months.append(s.strftime('%Y-%m-%d'))
            
            curr_inc = 0.0
            for t in txs:
                if t.category.type == 'Income': curr_inc += float(t.amount)
                elif t.category.type == 'Transfer' and t.category.name in ['Transfer Fidelity', 'Transfer Money Market', 'Transfer External'] and t.amount > 0: curr_inc += float(t.amount)
            incs.append(curr_inc)
            exps.append(float(abs(sum(t.amount for t in txs if t.category.type == 'Expense') or 0)))
            
        fig = go.Figure(data=[
            go.Bar(name='Income', x=months, y=incs, marker_color='#22c55e'), 
            go.Bar(name='Expense', x=months, y=exps, marker_color='#ef4444')
        ])
        fig.update_layout(title='Income vs Expenses (12 Mo)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=xaxis_date_config, shapes=event_shapes, annotations=event_annotations, margin=dict(t=60, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)

    # 5. SAVINGS TREND
    elif plot_type == 'savings_trend':
        savings_ids = [a.id for a in Account.query.filter_by(account_type='savings').all()]
        months, net_values, hover_texts, colors = [], [], [], []
        if savings_ids:
            for i in range(11, -1, -1):
                y, m = today.year, today.month - i
                while m <= 0: m += 12; y -= 1
                while m > 12: m -= 12; y += 1
                s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
                
                txs = Transaction.query.join(Category).filter(Transaction.date >= s, Transaction.date <= e, Transaction.account_id.in_(savings_ids), Transaction.is_deleted == False, Category.name != excluded_cat).all()
                
                in_val = float(sum(t.amount for t in txs if t.amount > 0 and (t.category.type == 'Transfer' or t.category.type == 'Income')) or 0)
                out_val = float(abs(sum(t.amount for t in txs if t.amount < 0 and t.category.type == 'Transfer')) or 0)
                net_change = in_val - out_val
                
                # FIX: Use ISO Date
                months.append(s.strftime('%Y-%m-%d'))
                
                net_values.append(net_change)
                colors.append('#10b981' if net_change >= 0 else '#ef4444')
                hover_texts.append(f"Net: ${net_change:,.2f}<br>In: ${in_val:,.2f}<br>Out: ${out_val:,.2f}")

        fig = go.Figure()
        fig.add_trace(go.Bar(x=months, y=net_values, marker_color=colors, text=net_values, texttemplate='$%{y:,.0f}', textposition='auto', hoverinfo='text', hovertext=hover_texts))
        fig.update_layout(title='Monthly Net Savings (Growth vs. Drawdown)', yaxis=dict(title='Net Change ($)', tickformat="$,.0f"), xaxis=xaxis_date_config, showlegend=False, shapes=event_shapes, annotations=event_annotations, margin=dict(t=60, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)

    # 6. SUSTAINABILITY
    elif plot_type == 'sustainability_trend':
        months, net_operating, cumulative_trend, colors, hover_texts = [], [], [], [], []
        running_total = 0.0
        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0: m += 12; y -= 1
            while m > 12: m -= 12; y += 1
            s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            txs = Transaction.query.join(Category).filter(Transaction.date >= s, Transaction.date <= e, Transaction.is_deleted == False, Category.name != excluded_cat).all()
            inflow = float(sum(t.amount for t in txs if t.amount > 0 and is_valid(t)) or 0)
            outflow = float(abs(sum(t.amount for t in txs if t.amount < 0 and is_valid(t)) or 0))
            monthly_net = inflow - outflow
            running_total += monthly_net 
            
            # FIX: Use ISO Date
            months.append(s.strftime('%Y-%m-%d'))
            
            net_operating.append(monthly_net)
            cumulative_trend.append(running_total)
            colors.append('#10b981' if monthly_net >= 0 else '#ef4444')
            hover_texts.append(f"Net: ${monthly_net:,.0f}<br>In: ${inflow:,.0f}<br>Out: ${outflow:,.0f}")
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Monthly Net', x=months, y=net_operating, marker_color=colors, text=net_operating, texttemplate='$%{y:,.0f}', textposition='auto', opacity=0.6, hoverinfo='text', hovertext=hover_texts))
        fig.add_trace(go.Scatter(name='Cumulative Cash Trend', x=months, y=cumulative_trend, mode='lines+markers', line=dict(color='#f59e0b', width=4), marker=dict(size=6)))
        fig.update_layout(title='Total Cash Flow (Excluding CC Payments)', yaxis=dict(title='Net Change ($)', tickformat="$,.0f"), xaxis=xaxis_date_config, showlegend=False, shapes=event_shapes, annotations=event_annotations, margin=dict(t=60, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)

    # 7. CATEGORY BREAKDOWN
    elif plot_type == 'category_breakdown':
        months = []
        cats = [c.name for c in Category.query.filter_by(type='Expense').all()]
        cat_data = {c: [] for c in cats}
        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0: m += 12; y -= 1
            while m > 12: m -= 12; y += 1
            s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            
            # FIX: Use ISO Date
            months.append(s.strftime('%Y-%m-%d'))
            
            res = db.session.query(Category.name, func.sum(Transaction.amount)).join(Transaction).filter(Transaction.date>=s, Transaction.date<=e, Category.type=='Expense', Transaction.is_deleted==False).group_by(Category.name).all()
            res_map = {r[0]: abs(float(r[1] or 0)) for r in res if r[0] != 'Transfer Credit Card Payment'}
            for c in cats: cat_data[c].append(res_map.get(c, 0))
        traces = [go.Bar(name=c, x=months, y=vals) for c, vals in cat_data.items() if sum(vals) > 0]
        fig = go.Figure(data=traces)
        fig.update_layout(title='Expenses by Category (12 Mo)', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=xaxis_date_config, shapes=event_shapes, annotations=event_annotations, margin=dict(t=60, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)

    # 8. TOP PAYEES (No changes needed, horizontal bar uses values on X-axis)
    elif plot_type == 'top_payees':
        today = date.today()
        y, m = today.year, today.month - 11
        while m <= 0: m += 12; y -= 1
        s = date(y, m, 1)
        display_name_expr = func.coalesce(PayeeRule.display_name, Payee.name)
        res = db.session.query(display_name_expr.label('d'), func.sum(Transaction.amount)).select_from(Transaction).join(Payee).outerjoin(PayeeRule).join(Category).filter(Transaction.date >= s, Transaction.date <= today, Category.type == 'Expense', Transaction.is_deleted == False, Category.name != excluded_cat).group_by(display_name_expr).order_by(func.sum(Transaction.amount).asc()).limit(20).all()
        filtered_res = [r for r in res if r[1] is not None]
        names, vals = [r[0] for r in filtered_res], [abs(float(r[1] or 0)) for r in filtered_res]
        names, vals = names[::-1], vals[::-1]
        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#6366f1', text=vals, texttemplate='$%{x:,.0f}')])
        fig.update_layout(title='Top 20 Payees (Last 12 Mo)', xaxis=dict(title='Total Spent', tickformat="$,.0f"), margin=dict(t=40, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)

    # 9. YOY COMPARISON (Uses categorical Jan-Dec, so we use xaxis_cat_config)
    elif plot_type == 'yoy_comparison':
        today = date.today()
        def get_data(yr):
            res = db.session.query(func.extract('month', Transaction.date).label('m'), func.sum(Transaction.amount).label('total'), Category.name).join(Category).filter(func.extract('year', Transaction.date) == yr, Category.type == 'Expense', Transaction.is_deleted == False).group_by('m', Category.name).all()
            monthly_totals = {}
            for r in res:
                if r.name == 'Transfer Credit Card Payment': continue
                month_idx = int(r.m)
                monthly_totals[month_idx] = monthly_totals.get(month_idx, 0) + abs(float(r.total or 0))
            return [monthly_totals.get(m, 0.0) for m in range(1, 13)]
        curr, last = get_data(today.year), get_data(today.year - 1)
        fin_curr = [curr[i] if i < today.month else None for i in range(12)]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=list(calendar.month_name[1:]), y=last, mode='lines+markers', name=f'{today.year-1}', line=dict(color='gray', dash='dash')))
        fig.add_trace(go.Scatter(x=list(calendar.month_name[1:]), y=fin_curr, mode='lines+markers', name=f'{today.year}', line=dict(color='#6366f1', width=3)))
        
        # FIX: Use CATEGORY config for Jan-Dec labels
        fig.update_layout(title='YoY Expenses', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=xaxis_cat_config, margin=dict(t=40, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)
    
    # 10. FLOW HEALTH
    elif plot_type == 'flow_health':
        months, upstream, downstream = [], [], []
        good_keywords = ['checking->savings', 'to savings', 'to fidelity', 'checking->fidelity']
        bad_keywords = ['savings->checking', 'from savings', 'fidelity->savings', 'fidelity->checking', 'from fidelity']
        strategic_keywords = ['car payoff', 'strategic', 'large purchase']
        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0: m += 12; y -= 1
            while m > 12: m -= 12; y += 1
            s, e = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            txs = Transaction.query.join(Category).filter(Transaction.date >= s, Transaction.date <= e, Transaction.is_deleted == False, Category.type == 'Transfer', Category.name != excluded_cat).all()
            good_sum, bad_sum = 0.0, 0.0
            for t in txs:
                cat_name = t.category.name.lower()
                amt = abs(float(t.amount or 0))
                if any(k in cat_name for k in strategic_keywords): continue
                if any(k in cat_name for k in bad_keywords): bad_sum += amt
                elif any(k in cat_name for k in good_keywords): good_sum += amt
            
            # FIX: Use ISO Date
            months.append(s.strftime('%Y-%m-%d'))
            
            upstream.append(good_sum)
            downstream.append(-bad_sum)
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Building Reserves', x=months, y=upstream, marker_color='#10b981', text=upstream, texttemplate='$%{y:,.0f}', textposition='auto'))
        fig.add_trace(go.Bar(name='Tapping Reserves', x=months, y=downstream, marker_color='#ef4444', text=downstream, texttemplate='$%{y:,.0f}', textposition='auto'))
        fig.update_layout(title='Directional Flow (Reserve Building vs. Tapping)', yaxis=dict(title='Flow Volume ($)', tickformat="$,.0f"), barmode='relative', xaxis=xaxis_date_config, shapes=event_shapes, annotations=event_annotations, margin=dict(t=60, b=20, l=50, r=10), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        return to_json(fig, pretty=True)

    return None

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
        last_month_end = today.replace(day=1) - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        prior_month_end = last_month_start - timedelta(days=1)
        prior_month_start = prior_month_end.replace(day=1)

        lm_spend, lm_net = get_spending_data_for_period(last_month_start, last_month_end)
        pm_spend, pm_net = get_spending_data_for_period(prior_month_start, prior_month_end)
        
        return jsonify({
            "last_month_name": last_month_start.strftime('%B %Y'),
            "last_month_spending": lm_spend, "prior_month_spending": pm_spend,
            "last_month_net": lm_net, "prior_month_net": pm_net
        })
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/yearly_insight_data')
def api_yearly_insight_data():
    try:
        today = date.today()
        cur_start = today.replace(day=1, month=1)
        cur_end = today
        last_end = today.replace(year=today.year - 1)
        last_start = last_end.replace(day=1, month=1)

        cur_spend, cur_net = get_spending_data_for_period(cur_start, cur_end)
        last_spend, last_net = get_spending_data_for_period(last_start, last_end)
        
        return jsonify({
            "current_ytd_spending": cur_spend, "last_ytd_spending": last_spend,
            "current_ytd_net": cur_net, "last_ytd_net": last_net,
            "current_year": today.year, "last_year": today.year - 1
        })
    except Exception as e: return jsonify({'error': str(e)}), 500

# --- ROUTES ---

@app.route('/', defaults={'month_offset': '0'})
@app.route('/<string:month_offset>')
def index(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    summary = get_monthly_summary(month_offset)
    
    return render_template('index.html', 
                           month_offset=month_offset, summary=summary,
                           plot_json_1=create_plot('income_vs_expense', summary),
                           plot_json_broad=create_plot('category_breakdown', summary), 
                           plot_json_3=create_plot('top_payees', summary),
                           plot_json_4=create_plot('yoy_comparison', summary),
                           plot_json_savings=create_plot('savings_trend', summary), # NEW
                           plot_json_sustain=create_plot('sustainability_trend', summary), # NEW
                           plot_json_groc=create_plot('groceries_vs_dining', summary), # NEW
                           plot_json_core=create_plot('core_operating', summary),
                           accounts=Account.query.all(), gemini_api_key=os.getenv('GEMINI_API_KEY'))

@app.route('/upload_file', methods=['POST'])
def upload_file():
    account_id = request.form.get('account_id')
    files = request.files.getlist('file')
    if not account_id or not files: return redirect(url_for('index'))
    
    account = Account.query.get_or_404(account_id)
    uncat_id = get_uncategorized_id()
    
    count_added = 0
    count_skipped = 0
    
    for file in files:
        try:
            df = pd.DataFrame()
            if file.filename.lower().endswith('.csv'):
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                df = pd.read_csv(stream)
                df.rename(columns=lambda x: x.strip(), inplace=True)
                # Handle flexible headers
                if 'Post Date' in df.columns: df.rename(columns={'Post Date': 'Date'}, inplace=True)
                if 'Memo' in df.columns: df.rename(columns={'Memo': 'Description'}, inplace=True)
            elif file.filename.lower().endswith('.pdf'):
                data = parse_chase_pdf(file.stream)
                if data: df = pd.DataFrame(data)
            
            if df.empty: continue
            
            # Check for required columns
            if not all(col in df.columns for col in ['Date', 'Amount', 'Description']):
                flash(f"File {file.filename} is missing one of the required columns (Date, Amount, Description).", "danger")
                continue

            for _, row in df.iterrows():
                try:
                    dvals = row.get('Date')
                    if isinstance(dvals, str): date_val = pd.to_datetime(dvals).date()
                    else: date_val = dvals
                    
                    desc = str(row.get('Description', '')).strip()
                    
                    amt = row.get('Amount')
                    amount = float(str(amt).replace('$','').replace(',',''))
                    if account.account_type == 'credit_card' and amount > 0: amount = -amount
                    
                    # --- DUPLICATE CHECK ---
                    # Check if this exact transaction already exists for this account
                    existing_tx = Transaction.query.filter_by(
                        account_id=account.id,
                        date=date_val,
                        amount=amount,
                        original_description=desc
                    ).first()

                    if existing_tx:
                        count_skipped += 1
                        continue # Skip this row
                    
                    # --- PAYEE & CATEGORY LOGIC ---
                    clean_name = desc.title()
                    payee = Payee.query.filter_by(name=clean_name).first()
                    
                    if not payee:
                        # New Payee -> Create
                        payee = Payee(name=clean_name)
                        db.session.add(payee)
                        db.session.commit()
                    
                    # Now, apply rules to find the category
                    cat_id = apply_rules_to_payee(payee, uncat_id)
                        
                    tx = Transaction(date=date_val, original_description=desc, amount=amount,
                                     payee_id=payee.id, category_id=cat_id, account_id=account.id)
                    db.session.add(tx)
                    count_added += 1
                except Exception as e: print(f"Row Error: {e}")
            db.session.commit()
        except Exception as e: 
            db.session.rollback()
            flash(f"File Error {file.filename}: {e}", "danger")
            
    flash(f"Imported {count_added} transactions. Skipped {count_skipped} duplicates.", "success")
    return redirect(url_for('index'))

# --- MANAGE CATEGORIES ---

@app.route('/manage_categories', methods=['GET', 'POST'])
def manage_categories():
    if request.method == 'POST':
        name = request.form.get('name')
        ctype = request.form.get('type')
        if name and ctype:
            if not Category.query.filter_by(name=name).first():
                db.session.add(Category(name=name, type=ctype))
                db.session.commit()
                flash("Category added.", "success")
        return redirect(url_for('manage_categories'))
    
    cats = Category.query.order_by(Category.type, Category.name).all()
    return render_template('manage_categories.html', categories=cats)

@app.route('/manage_categories/delete/<int:cat_id>', methods=['POST'])
def delete_category(cat_id):
    # Reassign to Uncategorized before delete
    uncat_id = get_uncategorized_id()
    if not uncat_id:
        flash("Critical Error: 'Uncategorized' category missing.", "danger")
        return redirect(url_for('manage_categories'))

    if cat_id == uncat_id:
        flash("Cannot delete default category.", "danger")
        return redirect(url_for('manage_categories'))
        
    Transaction.query.filter_by(category_id=cat_id).update({'category_id': uncat_id})
    # Also unlink PayeeRules that defaulted to this
    PayeeRule.query.filter_by(category_id=cat_id).update({'category_id': uncat_id})
    
    Category.query.filter_by(id=cat_id).delete()
    db.session.commit()
    return redirect(url_for('manage_categories'))

# --- TRANSACTIONS EDITING ---

@app.route('/edit_transactions', defaults={'month_offset': '0'}, methods=['GET','POST'])
@app.route('/edit_transactions/<string:month_offset>', methods=['GET','POST'])
def edit_transactions(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    
    # Get standard monthly data (for the nav bar context)
    summary = get_monthly_summary(month_offset)
    search_query = request.args.get('search', '').strip()

    if search_query:
        # GLOBAL SEARCH MODE
        # Ignore date range, search entire database
        current_context = f"Search Results: '{search_query}'"
        
        query = Transaction.query.join(Payee).filter(
            or_(
                Payee.name.ilike(f"%{search_query}%"), 
                Transaction.original_description.ilike(f"%{search_query}%")
            )
        )
    else:
        # STANDARD MONTHLY MODE
        # Filter by specific month range
        current_context = summary['current_month_name']
        
        query = Transaction.query.join(Payee).filter(
            Transaction.date >= summary['start_date'], 
            Transaction.date <= summary['end_date']
        )

    # Execute Query (Ordered by Date Descending)
    txs = query.order_by(Transaction.date.desc()).all()
    
    # Group categories for dropdown
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped = {}
    for c in cats:
        if c.type not in grouped: grouped[c.type] = []
        grouped[c.type].append(c)
        
    return render_template('edit_transactions.html', 
                           transactions=txs, 
                           payees=Payee.query.order_by(Payee.name).all(), 
                           grouped_categories=grouped, 
                           accounts=Account.query.all(), 
                           month_offset=month_offset, 
                           current_month_name=current_context, # Updates header title
                           search_query=search_query)

@app.route('/update_transaction/<int:t_id>', methods=['POST'])
def update_transaction(t_id):
    t = Transaction.query.get(t_id)
    if t:
        # Update Payee Logic
        name = request.form.get('payee_name_input').title()
        payee = Payee.query.filter_by(name=name).first()
        if not payee:
            payee = Payee(name=name)
            db.session.add(payee)
            db.session.commit()
            
        # Update Fields
        t.payee_id = payee.id
        t.category_id = int(request.form.get('category_id'))
        t.account_id = int(request.form.get('account_id'))
        t.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        t.amount = float(request.form.get('amount'))
        t.is_deleted = 'is_deleted' in request.form
        db.session.commit()
    
    # Redirect: Preserve month AND search query
    return redirect(url_for('edit_transactions', 
                            month_offset=request.form.get('month_offset'),
                            search=request.form.get('search_query')))

@app.route('/manage_categories/rename', methods=['POST'])
def rename_category():
    cat_id = request.form.get('category_id')
    new_name = request.form.get('category_name', '').strip()
    
    if not cat_id or not new_name:
        flash("Invalid data.", "danger")
        return redirect(url_for('manage_categories'))
        
    category = Category.query.get(cat_id)
    if not category:
        flash("Category not found.", "danger")
        return redirect(url_for('manage_categories'))

    # PROTECTION: Prevent renaming default
    if category.name == 'Uncategorized':
        flash("Cannot rename the default 'Uncategorized' category.", "danger")
        return redirect(url_for('manage_categories'))

    # Check for duplicate names
    existing = Category.query.filter(Category.name == new_name, Category.id != cat_id).first()
    if existing:
        flash(f"Category '{new_name}' already exists.", "danger")
    else:
        category.name = new_name
        db.session.commit()
        flash(f"Category renamed to '{new_name}'.", "success")
        
    return redirect(url_for('manage_categories'))

@app.route('/categorize', methods=['GET','POST'])
def categorize():
    # --- POST: SAVE RULE ---
    if request.method == 'POST':
        t = db.session.get(Transaction, request.form.get('transaction_id'))
        cat = int(request.form.get('category_id'))
        frag = request.form.get('rule_fragment', '').strip().upper()
        disp = request.form.get('payee_display_name', '').strip()
        
        # Capture ALL filter states to preserve view
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

    # --- GET: SHOW TRANSACTIONS ---
    filt = request.args.get('filter_type', 'all')
    srch = request.args.get('search', '').strip()
    year = request.args.get('year', '')
    month = request.args.get('month', '')

    q = Transaction.query.join(Payee).filter(Transaction.is_deleted == False)
    
    # Logic: If searching, include categorized items. If not, show only uncategorized.
    if not srch:
        q = q.filter(Payee.rule_id == None)
        
    # Apply Filters
    if filt == 'positive': q = q.filter(Transaction.amount > 0)
    elif filt == 'negative': q = q.filter(Transaction.amount < 0)
    
    if year and year != 'all':
        q = q.filter(extract('year', Transaction.date) == int(year))
    if month and month != 'all':
        q = q.filter(extract('month', Transaction.date) == int(month))

    if srch: 
        q = q.filter(or_(Payee.name.ilike(f"%{srch}%"), Transaction.original_description.ilike(f"%{srch}%")))

    # Fetch Data
    limit = 500 if (srch or year or month) else 50
    txs = q.order_by(Transaction.date.desc()).limit(limit).all()
    
    # Context Data
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped = {}
    for c in cats:
        if c.type not in grouped: grouped[c.type] = []
        grouped[c.type].append(c)
        
    labels = [r.display_name for r in db.session.query(PayeeRule.display_name).distinct().order_by(PayeeRule.display_name).all()]
    
    # Get available years for dropdown
    available_years = db.session.query(extract('year', Transaction.date)).distinct().order_by(extract('year', Transaction.date).desc()).all()
    available_years = [int(y[0]) for y in available_years] # Flatten tuple list

    return render_template('categorize.html', 
                           transactions=txs, 
                           grouped_categories=grouped, 
                           filter_type=filt, 
                           search_query=srch, 
                           selected_year=year,
                           selected_month=month,
                           available_years=available_years,
                           existing_labels=labels)

# --- MANAGE RULES & PAYEES ---

@app.route('/manage_rules', methods=['GET','POST'])
def manage_rules():
    if request.method == 'POST':
        rule_id = request.form.get('rule_id')
        frag = request.form.get('fragment', '').strip().upper()
        disp = request.form.get('display_name', '').strip()
        cat = request.form.get('category_id')
        
        if rule_id:
            # Edit existing rule
            r = db.session.get(PayeeRule, rule_id)
            if r: 
                r.fragment = frag
                r.display_name = disp
                r.category_id = cat
                # If editing, we usually don't force overwrite unless clicked specifically,
                # but we DO want to catch unlinked items.
                run_rule_on_all_payees(r, overwrite=False)
        elif not PayeeRule.query.filter_by(fragment=frag).first():
            # Create new rule
            new_rule = PayeeRule(fragment=frag, display_name=disp, category_id=cat)
            db.session.add(new_rule)
            db.session.commit() # Commit to get the ID
            # Run immediately on unlinked items
            run_rule_on_all_payees(new_rule, overwrite=False)
            
        db.session.commit()
        return redirect(url_for('manage_rules'))
    
    # GET Request (Search & List)
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

@app.route('/manage_rules/run_all', methods=['POST'])
def run_all_rules_force():
    """Fetches all rules and runs the overwrite logic top-to-bottom."""
    # Order by ID asc to simulate top-to-bottom priority based on creation time
    rules = PayeeRule.query.order_by(PayeeRule.id.asc()).all()
    total_payees_updated = 0
    
    for rule in rules:
        # Call the helper with overwrite=True to fix existing wrong links
        count = run_rule_on_all_payees(rule, overwrite=True)
        total_payees_updated += count
        
    flash(f"✅ Successfully ran {len(rules)} rules. Total payees forcibly linked/updated: {total_payees_updated}.", "success")
    return redirect(url_for('manage_rules'))


@app.route('/manage_rules/apply/<int:rule_id>', methods=['POST'])
def apply_rule_force(rule_id):
    rule = db.session.get(PayeeRule, rule_id)
    if rule:
        # Call the helper with overwrite=True
        count = run_rule_on_all_payees(rule, overwrite=True)
        flash(f"Rule '{rule.display_name}' forcibly applied to {count} payees.", "success")
    else:
        flash("Rule not found.", "danger")
    return redirect(url_for('manage_rules'))

@app.route('/manage_rules/delete/<int:rule_id>', methods=['POST'])
def delete_rule(rule_id):
    """Deletes a rule and unlinks payees."""
    rule = PayeeRule.query.get(rule_id)
    if rule:
        # 1. Unlink all Payees from this rule
        Payee.query.filter_by(rule_id=rule.id).update({'rule_id': None})
        
        # 2. Set all transactions from those payees to 'Uncategorized'
        uncat_id = get_uncategorized_id()
        payee_ids = [p.id for p in rule.payees]
        Transaction.query.filter(
            Transaction.payee_id.in_(payee_ids)
        ).update({'category_id': uncat_id})

        # 3. Delete the rule itself
        db.session.delete(rule)
        db.session.commit()
        flash(f"Rule '{rule.fragment}' deleted. All linked payees are now uncategorized.", "success")
    else:
        flash("Rule not found.", "danger")
        
    return redirect(url_for('manage_rules'))

@app.route('/manage_payees')
def manage_payees():
    """Page to view all payees and manually link/unlink them."""
    search_query = request.args.get('search', '').strip()
    
    # Get all rules for the dropdown
    rules = PayeeRule.query.order_by(PayeeRule.display_name).all()
    
    # Base Query
    query = db.session.query(
        Payee, 
        PayeeRule.display_name, 
        Category.name.label('category_name')
    ).select_from(Payee).outerjoin(PayeeRule).outerjoin(Category)

    # Apply Search Filter
    if search_query:
        term = f"%{search_query}%"
        query = query.filter(
            or_(
                Payee.name.ilike(term),
                PayeeRule.display_name.ilike(term)
            )
        )

    # Order results: Unlinked first, then alphabetical
    payees = query.order_by(
        case((Payee.rule_id == None, 1), else_=0).desc(),
        Payee.name
    ).all()

    return render_template('manage_payees.html', 
                           payees_data=payees, 
                           rules=rules,
                           search_query=search_query) # Pass query back to template

@app.route('/manage_payees/link', methods=['POST'])
def link_payee():
    """API endpoint to link a payee to a rule."""
    payee_id = request.form.get('payee_id')
    rule_id = request.form.get('rule_id') # 'None' if unlinking
    
    payee = Payee.query.get(payee_id)
    if not payee:
        return jsonify({'success': False, 'message': 'Payee not found.'})

    new_rule_id = None
    new_category_id = get_uncategorized_id()
    new_category_name = 'Uncategorized'
    new_rule_name = ''

    if rule_id and rule_id != 'None':
        rule = PayeeRule.query.get(rule_id)
        if rule:
            new_rule_id = rule.id
            new_category_id = rule.category_id
            new_category_name = rule.category.name
            new_rule_name = rule.display_name
        else:
            return jsonify({'success': False, 'message': 'Rule not found.'})

    # Update the payee
    payee.rule_id = new_rule_id
    
    # Update all of that payee's transactions
    Transaction.query.filter_by(payee_id=payee_id).update(
        {'category_id': new_category_id},
        synchronize_session=False
    )
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Payee updated.',
        'new_rule_name': new_rule_name,
        'new_category_name': new_category_name
    })

@app.route('/manage_payees/add', methods=['POST'])
def add_payee():
    """ADD PAYEE"""
    name = request.form.get('new_payee_name', '').strip().title()
    if name:
        if Payee.query.filter_by(name=name).first():
            flash("Payee already exists.", "danger")
        else:
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
    """DELETE PAYEE"""
    payee = Payee.query.get(payee_id)
    if not payee:
        flash("Payee not found.", "danger")
        return redirect(url_for('manage_payees'))
        
    # Check for existing transactions
    tx_count = Transaction.query.filter_by(payee_id=payee_id).count()
    if tx_count > 0:
        flash(f"Cannot delete payee '{payee.name}' because it has {tx_count} associated transactions.", "danger")
    else:
        db.session.delete(payee)
        db.session.commit()
        flash(f"Payee '{payee.name}' deleted.", "success")
        
    return redirect(url_for('manage_payees'))

# --- ACCOUNTS & EVENTS ---

@app.route('/edit_accounts', methods=['GET'])
def edit_accounts():
    accounts = Account.query.order_by(Account.name).all()
    for acc in accounts:
        net = db.session.query(func.sum(Transaction.amount)).filter(Transaction.account_id==acc.id, Transaction.is_deleted==False).scalar() or 0
        acc.current_balance = float(acc.starting_balance) + float(net)
    return render_template('edit_account_balances.html', accounts=accounts, account_types=['checking','savings','credit_card'])

@app.route('/update_account', methods=['POST'])
def update_account():
    acc = Account.query.get(request.form.get('account_id'))
    acc.name = request.form.get('name')
    acc.account_type = request.form.get('account_type')
    acc.starting_balance = float(request.form.get('starting_balance'))
    db.session.commit()
    return redirect(url_for('edit_accounts'))

@app.route('/add_account', methods=['POST'])
def add_account():
    name = request.form.get('account_name')
    atype = request.form.get('account_type')
    
    if Account.query.filter_by(name=name, account_type=atype).first():
        flash(f"Account '{name}' of type '{atype}' already exists!", "danger")
    else:
        db.session.add(Account(name=name, account_type=atype, starting_balance=0))
        db.session.commit()
        flash(f"Account '{name}' added.", "success")
        
    return redirect(url_for('edit_accounts'))

@app.route('/delete_account/<int:acc_id>', methods=['POST'])
def delete_account(acc_id):
    acc = Account.query.get(acc_id)
    if acc:
        db.session.delete(acc) # Transactions will cascade delete
        db.session.commit()
        flash(f"Account '{acc.name}' and all its transactions deleted.", "success")
    return redirect(url_for('edit_accounts'))

@app.route('/calculate_starting_balance/<int:account_id>', methods=['POST'])
def calculate_starting_balance(account_id):
    try:
        data = request.get_json()
        current_actual_balance = float(data.get('current_actual_balance', '0').replace('$', '').replace(',', ''))
        
        # Get net change
        net_change = db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.account_id == account_id,
            Transaction.is_deleted == False
        ).scalar() or 0.0

        # new_starting_balance + net_change = current_actual_balance
        new_starting_balance = current_actual_balance - float(net_change)
        
        return jsonify({
            'success': True, 
            'new_starting_balance': f"{new_starting_balance:.2f}"
        })

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/events')
def events():
    return render_template('events.html', events=Event.query.order_by(Event.date.desc()).all())

@app.route('/add_event', methods=['POST'])
def add_event():
    date_val = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
    desc = request.form.get('description')
    
    if Event.query.filter_by(date=date_val, description=desc).first():
        flash(f"Event '{desc}' on {date_val} already exists!", "danger")
    else:
        db.session.add(Event(date=date_val, description=desc))
        db.session.commit()
        flash(f"Event '{desc}' added.", "success")
        
    return redirect(url_for('events'))

@app.route('/delete_event/<int:eid>', methods=['POST'])
def delete_event(eid):
    Event.query.filter_by(id=eid).delete()
    db.session.commit()
    return redirect(url_for('events'))

# --- TRENDS ---

@app.route('/trends')
def trends():
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped_categories = {}
    for c in cats:
        if c.type not in grouped_categories: grouped_categories[c.type] = []
        grouped_categories[c.type].append(c)

    # FIX: Query only DISTINCT Display Names (Labels)
    # This collapses "Aldi #1" and "Aldi #2" into just "Aldi" if they share a rule
    results = db.session.query(
        func.coalesce(PayeeRule.display_name, Payee.name).label('label')
    ).select_from(Payee).outerjoin(PayeeRule).distinct().order_by('label').all()
    
    # Convert list of tuples [('Aldi',), ('Uber',)] to flat list ['Aldi', 'Uber']
    unique_labels = [r.label for r in results if r.label]

    return render_template('trends.html', 
                           grouped_categories=grouped_categories, 
                           all_events=Event.query.all(), 
                           payee_labels=unique_labels) # Passing strings, not objects

@app.route('/api/trend_data', methods=['POST'])
def get_trend_data():
    data = request.get_json()
    cat_ids = data.get('category_ids', [])
    payee_names = data.get('payee_names', []) # Using names/labels now
    evt_ids = [int(i) for i in data.get('event_ids', [])]
    s_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
    e_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
    bucket = data.get('time_bucket', 'month')
    
    chart_type_map = {'line': 'scatter', 'area': 'scatter', 'bar': 'bar'}
    chart_type_key = data.get('chart_type', 'bar')
    chart_type = chart_type_map.get(chart_type_key, 'bar')
    fill_mode = 'tozeroy' if chart_type_key == 'area' else 'none'

    data_by_item, all_buckets = {}, set()
    
    # --- Category Logic ---
    if cat_ids:
        bucket_col = func.date_trunc(bucket, Transaction.date)
        
        query_cat = db.session.query(
            bucket_col.label('bucket_start'), 
            Category.name.label('name'), 
            func.sum(Transaction.amount).label('total')
        ).select_from(Transaction).join(Category).filter( # FIX: Added .select_from(Transaction)
            Transaction.date >= s_date, Transaction.date <= e_date, 
            Transaction.is_deleted == False, Transaction.category_id.in_(cat_ids)
        ).group_by(bucket_col, Category.name).order_by(bucket_col)
        
        results_cat = query_cat.all()
        
        for row in results_cat:
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            total = abs(float(row.total))
            all_buckets.add(b_date)
            if row.name not in data_by_item: data_by_item[row.name] = {}
            data_by_item[row.name][b_date] = data_by_item[row.name].get(b_date, 0) + total

    # --- Payee Logic ---
    if payee_names:
        bucket_col = func.date_trunc(bucket, Transaction.date)
        name_col = func.coalesce(PayeeRule.display_name, Payee.name)

        query_payee = db.session.query(
            bucket_col.label('bucket_start'), 
            name_col.label('name'), 
            func.sum(Transaction.amount).label('total')
        ).select_from(Transaction).join(Payee).outerjoin(PayeeRule).filter( # FIX: Added .select_from(Transaction)
            Transaction.date >= s_date, 
            Transaction.date <= e_date, 
            Transaction.is_deleted == False, 
            name_col.in_(payee_names)
        ).group_by(bucket_col, name_col).order_by(bucket_col)
        
        results_payee = query_payee.all()

        for row in results_payee:
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            total = abs(float(row.total))
            all_buckets.add(b_date)
            name = f"[P] {row.name}" # Add prefix to distinguish payees from categories
            if name not in data_by_item: data_by_item[name] = {}
            data_by_item[name][b_date] = data_by_item[name].get(b_date, 0) + total

    sorted_buckets = sorted(list(all_buckets))
    
    plot_data = []
    
    # Set mode based on chart type
    chart_mode = 'lines+markers' if chart_type_key == 'line' else None
    fill_type = 'tozeroy' if chart_type_key == 'area' else None

    for name, data_points in data_by_item.items():
        plot_data.append({
            'type': chart_type,
            'name': name,
            'x': sorted_buckets,
            'y': [data_points.get(b, 0) for b in sorted_buckets],
            'mode': chart_mode,
            'fill': fill_type
        })
    
    events = Event.query.filter(Event.date >= s_date, Event.date <= e_date, Event.id.in_(evt_ids)).all()
    shapes = [{'type': 'line', 'x0': e.date.strftime('%Y-%m-%d'), 'x1': e.date.strftime('%Y-%m-%d'), 'y0':0, 'y1':1, 'yref':'paper', 'line': {'color':'pink', 'dash':'dot'}} for e in events]
    anns = [{'x': e.date.strftime('%Y-%m-%d'), 'y':0.95, 'yref':'paper', 'text': e.description, 'showarrow':False, 'bgcolor':'rgba(255,192,203,0.8)'} for e in events]

    return jsonify({'plot_data': plot_data, 'layout_shapes': shapes, 'layout_annotations': anns})

@app.route('/api/min_max_dates')
def api_date_range():  # <--- Renamed from 'get_min_max_dates' to match trends.html
    min_date = db.session.query(func.min(Transaction.date)).scalar()
    max_date = db.session.query(func.max(Transaction.date)).scalar()
    return jsonify({
        'min_date': min_date.strftime('%Y-%m-%d') if min_date else date.today().strftime('%Y-%m-%d'),
        'max_date': max_date.strftime('%Y-%m-%d') if max_date else date.today().strftime('%Y-%m-%d')
    })

if __name__ == '__main__':
    with app.app_context():
        create_tables()
    app.run(debug=True)