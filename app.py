import os
import calendar
import re
import io
import json
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
# CONFIGURATION: How many months to show on dashboard charts
DASHBOARD_MONTH_SPAN = 12

EXCLUDED_CAT = 'Ignored Credit Card Payment'
EXCLUDED_CAT_CORE = ['Car Payment', 'VUL', 'AC Payment', 'Taxes']
EXCLUDED_PAYEE_LABELS_CORE = ['Planting Oaks', 'Jaxco Furniture','Planting Oaks', 'Step Up','Jiu Jitsu','Abeka','Christianbook'
                              ,'New Leaf Publishing','Veritas']

CHASE_DATE_REGEX = re.compile(r"Opening/Closing Date\s*([\d/]+)\s*-\s*([\d/]+)")
CHASE_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*)")
HSA_PERIOD_REGEX = re.compile(r"Period\s*:?\s*(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})\s*through\s*(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})", re.IGNORECASE)
AMOUNT_REGEX = re.compile(r"([\d,]+\.\d{2})(?=\s*$)")
HSA_LINE_REGEX = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*?)\s+([\(]?[\d,]+\.\d{2}[\)]?)\s+[\d,]+\.\d{2}")

#WF
WF_DATE_HEADER_REGEX = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+Page") # Matches "November 18, 2025 Page..."
WF_BEGIN_BAL_REGEX = re.compile(r"Beginning balance on (\d{1,2}/\d{1,2})")
WF_END_BAL_REGEX = re.compile(r"Ending balance on (\d{1,2}/\d{1,2})")
WF_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*?)\s+([\d,]+\.\d{2})")
# Matches line starting with MM/DD: "10/17 Blue Cross ... 3,665.45"
# Group 1: Date, Group 2: Description + Numbers
WF_TEXT_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*)")
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
    
class StatementRecord(db.Model, TimestampMixin):
    """Tracks uploaded PDF statement periods to prevent duplicates."""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False)
    
    account = db.relationship('Account', backref='statement_records', lazy=True)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'start_date', 'end_date', name='_statement_period_uc'),
    )

class Category(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True) # Unique creates an implicit index
    type = db.Column(db.String(20), nullable=False)
    transactions = db.relationship('Transaction', backref='category', lazy=True)
    payee_rules = db.relationship('PayeeRule', backref='category', lazy=True)
    # Removed redundant idx_category_name since name is unique

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
    name = db.Column(db.String(500), nullable=False, unique=True) # Unique creates an implicit index
    rule_id = db.Column(db.Integer, db.ForeignKey('payee_rule.id'), nullable=True)
    transactions = db.relationship('Transaction', backref='payee', lazy=True)
    # Removed redundant idx_payee_name since name is unique

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
        # Composite index for date filtering which usually also filters by is_deleted
        db.Index('idx_tx_date_deleted', 'date', 'is_deleted'),
        # Index Foreign Keys for faster joins
        db.Index('idx_tx_category', 'category_id'),
        db.Index('idx_tx_account', 'account_id'),
        db.Index('idx_tx_payee', 'payee_id'), # Added missing index for Payee Joins
    )

class Event(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500), nullable=False)
    __table_args__ = (db.UniqueConstraint('date', 'description', name='_event_uc'),)

class Budget(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    criteria = db.Column(db.Text, nullable=False)

# --- INITIALIZATION ---

def create_tables():
    db.create_all()

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

def try_parse_date(date_str):
    if not date_str: return None
    date_str = date_str.strip()
    
    # Try Standard ISO format first (HTML5 Date Input)
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        pass

    # Normalize separators for other formats
    clean_date_str = date_str.replace('.', '/').replace('-', '/')
    for fmt in ('%m/%d/%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(clean_date_str, fmt).date()
        except ValueError:
            continue
    return None

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
    """
    Returns a tuple: (transactions_list, period_start_date, period_end_date)
    or (None, None, None) on failure.
    """
    transactions = []
    period_start = None
    period_end = None
    try:
        with pdfplumber.open(file_stream) as pdf:
            # 1. Extract Date Range
            page1_text = pdf.pages[0].extract_text()
            date_match = CHASE_DATE_REGEX.search(page1_text)
            if not date_match: return None, None, None
            
            period_start = datetime.strptime(date_match.group(1), '%m/%d/%y').date()
            period_end = datetime.strptime(date_match.group(2), '%m/%d/%y').date()
            end_year, end_month = period_end.year, period_end.month

            # 2. Extract Transactions
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
        return transactions, period_start, period_end
    except Exception as e: 
        print(f"PDF Parse Error: {e}")
        return None, None, None
    
def parse_wellsfargo_pdf(file_stream):
    """
    Parses Wells Fargo PDF using Text Lines + Regex (No Tables).
    Looks for 'Transaction history' trigger, then parses lines with dates.
    """
    transactions = []
    period_start = None
    period_end = None
    statement_year = date.today().year
    
    transaction_section_found = False
    
    try:
        with pdfplumber.open(file_stream) as pdf:
            full_text = ""
            # Extract all text first
            for p in pdf.pages: full_text += p.extract_text() + "\n"

            # 1. Extract Statement Dates (Header/Summary)
            header_match = WF_DATE_HEADER_REGEX.search(full_text)
            if header_match:
                try:
                    dt_str = header_match.group(1)
                    statement_date_obj = datetime.strptime(dt_str, "%B %d, %Y").date()
                    statement_year = statement_date_obj.year
                except ValueError: pass
            
            start_match = WF_BEGIN_BAL_REGEX.search(full_text)
            end_match = WF_END_BAL_REGEX.search(full_text)
            if start_match and end_match:
                try:
                    p_start_str = f"{start_match.group(1)}/{statement_year}"
                    p_end_str = f"{end_match.group(1)}/{statement_year}"
                    period_start = datetime.strptime(p_start_str, "%m/%d/%Y").date()
                    period_end = datetime.strptime(p_end_str, "%m/%d/%Y").date()
                    if period_end < period_start:
                        period_start = period_start.replace(year=statement_year - 1)
                except ValueError: pass

            # 2. Parse Lines for Transactions
            lines = full_text.split('\n')
            
            for line in lines:
                # Trigger: Start parsing AFTER "Transaction history"
                if "Transaction history" in line:
                    transaction_section_found = True
                    continue
                
                if not transaction_section_found:
                    continue
                
                # Stop Trigger: Common end sections (Ending Daily Balance summary usually follows, but we can just rely on regex matches)
                # Optionally stop if "Monthly service fee summary"
                if "Monthly service fee summary" in line:
                    break

                # REGEX: Look for "MM/DD <Desc> <Amount(s)>"
                match = WF_TEXT_LINE_REGEX.search(line)
                if match:
                    date_str, rest = match.groups()
                    
                    # Attempt to find numbers at end of string
                    # Logic: Find all matches of numbers like "1,234.56" at end
                    # If 2 found: Transaction Amount | Balance
                    # If 1 found: Transaction Amount
                    
                    numbers = re.findall(r"([\d,]+\.\d{2})", rest)
                    if not numbers: continue
                    
                    # Last number is balance if there are 2+, second to last is amount
                    # Or if just 1, it is amount.
                    # BUT text extraction might put balance on next line.
                    # Let's assume the LAST number on the transaction line is the amount if only 1 number exists.
                    # If 2 numbers exist, the FIRST one is amount, SECOND is balance.
                    
                    try:
                        raw_amount_str = numbers[0]
                        if len(numbers) >= 2:
                            raw_amount_str = numbers[0] # First number is transaction, second is balance
                        
                        amount = float(raw_amount_str.replace(',', ''))
                        
                        # Clean description: remove the numbers from the end string
                        desc = rest.split(raw_amount_str)[0].strip()

                        # Direction Logic (Heuristic since text loses columns)
                        # Keywords for INCOME (Positive)
                        income_keywords = ['DEPOSIT', 'PAYROLL', 'TRANSFER FROM', 'INTEREST', 'ZELLE FROM', 'VENMO PAYMENT']
                        # Keywords for EXPENSE (Negative) - Default
                        
                        is_income = any(k in desc.upper() for k in income_keywords)
                        
                        if not is_income:
                            amount = -amount
                        
                        # Parse Date
                        t_month = int(date_str.split('/')[0])
                        t_year = statement_year
                        if period_end and period_end.month < 6 and t_month > 6:
                            t_year -= 1
                        
                        t_date = datetime.strptime(f"{date_str}/{t_year}", "%m/%d/%Y").date()
                        
                        transactions.append({'Date': t_date, 'Description': desc, 'Amount': amount})
                        
                    except ValueError: continue

        return transactions, period_start, period_end
    except Exception as e:
        print(f"WF PDF Parse Error: {e}")
        return None, None, None

def get_monthly_summary_direct(month_offset):
    today = date.today()
    idx = today.month - 1 + month_offset
    year, month = today.year + (idx // 12), (idx % 12) + 1
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    current_month_name = start.strftime('%B %Y')
    return {'start_date': start, 'end_date': end, 'current_month_name': current_month_name}

def parse_hsa_pdf(file_stream):
    """
    Returns a tuple: (transactions_list, period_start_date, period_end_date)
    """
    transactions = []
    period_start = None
    period_end = None
    try:
        with pdfplumber.open(file_stream) as pdf:
            full_text = ""
            for p in pdf.pages:
                txt = p.extract_text()
                if txt: full_text += txt + "\n"
            
            # 1. Extract Period (Flexible Year)
            # Matches text like: Period: 10/01/25 through 10/31/25
            
            period_match = HSA_PERIOD_REGEX.search(full_text)
            print(period_match)
            if period_match:
                period_start = try_parse_date(period_match.group(1))
                period_end = try_parse_date(period_match.group(2))

            lines = full_text.split('\n')
            for line in lines:
                match = HSA_LINE_REGEX.search(line)
                if match:
                    date_str, desc, amt_str = match.groups()
                    try:
                        dt = datetime.strptime(date_str, '%m/%d/%Y').date()
                        is_negative = '(' in amt_str or ')' in amt_str
                        clean_amt = amt_str.replace('(', '').replace(')', '').replace(',', '')
                        amount = float(clean_amt)
                        
                        if is_negative:
                            amount = -amount
                        
                        # STRICTLY IGNORE CONTRIBUTIONS (Positive numbers)
                        if amount >= 0:
                            continue

                        transactions.append({'Date': dt, 'Description': desc.strip(), 'Amount': amount})
                    except ValueError:
                        continue 
        return transactions, period_start, period_end
    except Exception as e:
        print(f"HSA PDF Parse Error: {e}")
        return None, None, None

def get_monthly_summary_direct(month_offset):
    today = date.today()
    idx = today.month - 1 + month_offset
    year, month = today.year + (idx // 12), (idx % 12) + 1
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    current_month_name = start.strftime('%B %Y')
    return {'start_date': start, 'end_date': end, 'current_month_name': current_month_name}

# --- DASHBOARD SERVICE ---

class DashboardService:
    def __init__(self, view_mode='monthly'):
        self.view_mode = view_mode
        
        # 1. Determine Core Anchor Date
        # Logic: Find the max date available for EACH core account type separately.
        # Then take the MINIMUM of those dates.
        # Example: Checking has Dec 11, CC has Nov 11 -> We use Nov 11.
        # This prevents the dashboard from showing a month where one major account is missing data.
        
        core_types = ['checking', 'savings', 'credit_card']
        
        # Get the max statement date for each account type present in the database
        type_max_dates = db.session.query(func.max(StatementRecord.end_date))\
            .join(Account)\
            .filter(Account.account_type.in_(core_types))\
            .group_by(Account.account_type)\
            .all()
            
        # type_max_dates returns a list of tuples like [(date(2025,11,11),), (date(2025,12,11),)]
        valid_dates = [d[0] for d in type_max_dates if d[0] is not None]
        
        if valid_dates:
            # We take the MIN of the MAXs
            self.today = min(valid_dates)
        else:
            # Fallback if no statements exist at all
            self.today = date.today()

        # 2. Determine HSA Anchor Date (Independent of Core)
        hsa_max_date = db.session.query(func.max(StatementRecord.end_date))\
            .join(Account)\
            .filter(Account.account_type == 'hsa').scalar()
            
        self.hsa_today = hsa_max_date if hsa_max_date else date.today()
        
        # Calculate start date based on configuration (using Core date)
        months_back = max(24, DASHBOARD_MONTH_SPAN + 6) 
        
        y_hist, m_hist = self.today.year, self.today.month - months_back
        while m_hist <= 0:
            m_hist += 12
            y_hist -= 1
        
        self.fetch_start_date = date(y_hist, m_hist, 1)
        
        # Fetch ALL transactions first
        all_txs = Transaction.query.options(
            joinedload(Transaction.category),
            joinedload(Transaction.account),
            joinedload(Transaction.payee).joinedload(Payee.rule)
        ).filter(
            Transaction.date >= self.fetch_start_date,
            Transaction.is_deleted == False
        ).all()

        self.events = Event.query.filter(Event.date >= self.fetch_start_date).all()
        self.savings_account_ids = {a.id for a in Account.query.filter_by(account_type='savings').all()}
        self.hsa_account_ids = {a.id for a in Account.query.filter_by(account_type='hsa').all()}

        self.core_transactions = [t for t in all_txs if t.account_id not in self.hsa_account_ids]
        self.hsa_transactions = [t for t in all_txs if t.account_id in self.hsa_account_ids]
        
        self.base_layout = dict(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#71717a'), autosize=True)
        self.margin_std = dict(t=40, b=20, l=50, r=10)
        self.margin_legend = dict(t=40, b=100, l=50, r=10) 
        self.margin_events = dict(t=60, b=20, l=50, r=10)
        
        # Update X-axis format based on view mode
        tick_fmt = '%b %Y' if self.view_mode == 'monthly' else '%b %d'
        self.xaxis_date = dict(rangeslider=dict(visible=True), type='date', tickformat=tick_fmt)
        self.xaxis_cat = dict(rangeslider=dict(visible=False), type='category')
    # [Keep get_summary_for_dashboard unchanged]
    def get_summary_for_dashboard(self, month_offset):
        # ... [Existing implementation] ...
        # (This logic is strictly "Current Month" regardless of chart view, so no changes needed)
        idx = self.today.month - 1 + month_offset
        year, month = self.today.year + (idx // 12), (idx % 12) + 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        current_month_name = start.strftime('%B %Y')
        
        txs = [t for t in self.core_transactions if start <= t.date <= end]
        
        inc = sum(t.amount for t in txs if t.category.type == 'Income' and t.category.name != EXCLUDED_CAT)
        exp = abs(sum(t.amount for t in txs if t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT))
        s_in = sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount > 0 and (t.category.type in ['Transfer', 'Income']))
        s_out = abs(sum(t.amount for t in txs if t.account_id in self.savings_account_ids and t.amount < 0 and t.category.type == 'Transfer'))
        net_worth = 0.0
        balances = {}
        
        uncat = Transaction.query.join(Payee).filter(Payee.rule_id == None, Transaction.is_deleted == False).count()
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
        """Helper to normalize dates to the start of the period (Month 1st or Monday)."""
        if self.view_mode == 'weekly':
            return date_obj - timedelta(days=date_obj.weekday())
        return date_obj.replace(day=1)

    def _get_event_overlays(self, start, end):
        relevant_events = [e for e in self.events if start <= e.date <= end]
        events_map = {}
        for e in relevant_events:
            # Snap event to the bucket start date
            key = self._get_period_key(e.date).strftime('%Y-%m-%d')
            if key not in events_map: events_map[key] = []
            events_map[key].append(e.description)
        shapes = [{'type': 'line', 'x0': k, 'x1': k, 'y0': 0, 'y1': 1, 'xref': 'x', 'yref': 'paper', 'line': {'color': '#9ca3af', 'width': 1.5, 'dash': 'dot'}} for k in events_map]
        anns = [{'x': k, 'y': 1.02, 'xref': 'x', 'yref': 'paper', 'text': "📍 " + "<br>📍 ".join(v), 'showarrow': False, 'xanchor': 'center', 'yanchor': 'bottom', 'font': {'size': 10, 'color': '#4b5563'}, 'align': 'center'} for k, v in events_map.items()] 
        return shapes, anns

    def _group_transactions(self, txs, start, end):
        """Performance: Group transactions by period key once (O(N)) instead of filtering in loops (O(N^2))."""
        groups = {}
        for t in txs:
            if start <= t.date <= end:
                key = self._get_period_key(t.date).strftime('%Y-%m-%d')
                if key not in groups: groups[key] = []
                groups[key].append(t)
        return groups
    
    def _chart_eat_out_patterns(self, start_date, end_date):
        # Initialize buckets for Mon(0) to Sun(6)
        dow_totals = {i: 0.0 for i in range(7)}
        dow_counts = {i: 0 for i in range(7)}
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        # Filter for 'Eat Out' transactions in the valid range
        txs = [t for t in self.core_transactions 
               if start_date <= t.date <= end_date 
               and t.category.name == 'Eat Out']
        
        for t in txs:
            idx = t.date.weekday() # 0=Mon, 6=Sun
            dow_totals[idx] += float(abs(t.amount))
            dow_counts[idx] += 1
            
        # Prepare Data for Plotly
        y_vals = [dow_totals[i] for i in range(7)]
        # Calculate Average Transaction size per day (optional, for hover text)
        avgs = [dow_totals[i]/dow_counts[i] if dow_counts[i] > 0 else 0 for i in range(7)]
        
        # Color scale: Highlight the highest spending day
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
        
        fig.update_layout(
            title='Eat Out Spending by Day of Week',
            yaxis=dict(title='Total Spent ($)', tickformat="$,.0f"),
            xaxis=dict(title=''),
            margin=self.margin_std,
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def generate_all_charts(self):
        # Calculate time range
        months_back = DASHBOARD_MONTH_SPAN - 1
        
        if self.view_mode == 'weekly':
            # For weekly, cover roughly the same timeframe (52 weeks ~ 1 year)
            start_date = self.today - timedelta(weeks=months_back * 4.3)
            # Align to Monday
            start_date = start_date - timedelta(days=start_date.weekday())
        else:
            y_start, m_start = self.today.year, self.today.month - months_back
            while m_start <= 0: 
                m_start += 12
                y_start -= 1
            start_date = date(y_start, m_start, 1)

        end_date = self.today
        if self.view_mode == 'monthly':
            end_date = date(self.today.year, self.today.month, calendar.monthrange(self.today.year, self.today.month)[1])
        
        # Generate buckets
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
        
        # Pre-group transactions
        grouped_core = self._group_transactions(self.core_transactions, start_date, end_date)
        
        shapes, anns = self._get_event_overlays(start_date, end_date)
        
        return {
            'chart_income_vs_expense': self._chart_income_vs_expense(periods, period_strs, grouped_core, shapes, anns),
            'chart_savings': self._chart_savings(periods, period_strs, grouped_core, shapes, anns),
            'chart_cash_flow': self._chart_cash_flow(periods, period_strs, grouped_core, shapes, anns),
            'chart_core_operating': self._chart_core_operating(periods, period_strs, grouped_core, shapes, anns),
            'chart_groceries': self._chart_groceries(periods, period_strs, grouped_core, shapes, anns),
            'chart_expense_broad': self._chart_expense_broad(periods, period_strs, grouped_core, shapes, anns),
            'chart_core_summary': self._chart_core_summary(periods, period_strs, grouped_core, shapes, anns),
            'chart_top_payees': self._chart_top_payees(self.core_transactions, start_date), # Logic differs (aggregate total), pass raw list
            'chart_yoy': self._chart_yoy(), # Explicitly stays Monthly per user request
            'chart_core_breakdown': self._chart_core_breakdown(periods, period_strs, grouped_core, shapes, anns),
            'chart_hsa_activity': self._chart_hsa_activity(periods, period_strs, start_date, end_date),
            'chart_eat_out_patterns': self._chart_eat_out_patterns(start_date, end_date)
        }

    # --- Refactored Chart Methods (Using Grouped Data) ---

    def _chart_hsa_activity(self, periods, period_strs, start_date, end_date):
        data_by_payee = {}
        # Pre-filter HSA transactions
        txs = [t for t in self.hsa_transactions if start_date <= t.date <= end_date and t.amount < 0]
        
        for t in txs:
            name = t.payee.rule.display_name if t.payee.rule else t.payee.name
            # Use shared logic for key generation
            m_key = self._get_period_key(t.date).strftime('%Y-%m-%d')
            
            if name not in data_by_payee: data_by_payee[name] = {}
            data_by_payee[name][m_key] = data_by_payee[name].get(m_key, 0.0) + float(abs(t.amount))
            
        traces = []
        for name in sorted(data_by_payee.keys(), reverse=True):
            y_vals = [data_by_payee[name].get(m, 0.0) for m in period_strs]
            traces.append(go.Bar(name=name, x=period_strs, y=y_vals))
            
        fig = go.Figure(data=traces)
        fig.update_layout(
            title='HSA Expenses by Payee', 
            yaxis=dict(title='Spent ($)', tickformat="$,.0f"), 
            xaxis=self.xaxis_date, 
            barmode='stack', 
            margin=self.margin_events, 
            **self.base_layout
        )
        return to_json(fig, pretty=True)

    def _chart_core_breakdown(self, periods, period_strs, grouped_txs, shapes, anns):
        cat_data = {}
        all_cats = set()
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            if p_str not in cat_data: cat_data[p_str] = {}
            temp_cat_totals = {}
            for t in p_txs:
                if t.category.type == 'Expense' and \
                   t.category.name not in EXCLUDED_CAT_CORE and \
                   (t.payee.rule.display_name if t.payee.rule else t.payee.name) not in EXCLUDED_PAYEE_LABELS_CORE:
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
        incs, exps = [], []
        cumulative_savings = 0.0 
        cumulative_data = []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            # Filter excluded cat
            p_txs = [t for t in p_txs if t.category.name != EXCLUDED_CAT]
            
            curr_inc = sum(t.amount for t in p_txs if t.category.type == 'Income')
            curr_exp = sum(t.amount for t in p_txs if t.category.type == 'Expense')
            incs.append(float(curr_inc))
            exps.append(float(abs(curr_exp)))
            monthly_net = float(curr_inc) - float(abs(curr_exp))
            cumulative_savings += monthly_net
            cumulative_data.append(cumulative_savings)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Income', x=period_strs, y=incs, marker_color='#22c55e'))
        fig.add_trace(go.Bar(name='Expense', x=period_strs, y=exps, marker_color='#ef4444'))
        fig.add_trace(go.Scatter(name='Cumulative Savings', x=period_strs, y=cumulative_data, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Income vs Expenses (Budget View)', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=dict(t=60, b=100, l=50, r=10), **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_savings(self, periods, period_strs, grouped_txs, shapes, anns):
        net_vals, hover_txt, colors = [], [], []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            p_txs = [t for t in p_txs if t.account_id in self.savings_account_ids and t.category.name != EXCLUDED_CAT]
            
            in_val = float(sum(t.amount for t in p_txs if t.amount > 0 and t.category.type in ['Transfer', 'Income']))
            out_val = float(abs(sum(t.amount for t in p_txs if t.amount < 0 and t.category.type == 'Transfer')))
            net = in_val - out_val
            net_vals.append(net)
            colors.append('#10b981' if net >= 0 else '#ef4444')
            hover_txt.append(f"Net: ${net:,.2f}<br>In: ${in_val:,.2f}<br>Out: ${out_val:,.2f}")
        fig = go.Figure(go.Bar(x=period_strs, y=net_vals, marker_color=colors, text=net_vals, texttemplate='$%{y:,.0f}', textposition='auto', hoverinfo='text', hovertext=hover_txt))
        fig.update_layout(title='Net Savings (Growth vs. Drawdown)', yaxis=dict(title='Net Change ($)', tickformat="$,.0f"), xaxis=self.xaxis_date, showlegend=False, shapes=shapes, annotations=anns, margin=self.margin_events, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_cash_flow(self, periods, period_strs, grouped_txs, shapes, anns):
        inc, exp, net = [], [], []
        cumulative_flow = 0.0 
        cumulative_data = []
        valid_transfers = ['Investment Transfer', 'Savings Transfer']
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            p_txs = [t for t in p_txs if t.category.name != EXCLUDED_CAT]
            
            i_val = sum(t.amount for t in p_txs if (t.category.type == 'Income') or (t.amount > 0 and t.category.name in valid_transfers) or (t.amount > 0 and t.category.type == 'Expense')) 
            e_val = sum(t.amount for t in p_txs if (t.category.type == 'Expense' and t.amount < 0) or (t.amount < 0 and t.category.name in valid_transfers))
            i_val = float(i_val)
            e_val = float(abs(e_val))
            monthly_net = i_val - e_val
            cumulative_flow += monthly_net
            inc.append(i_val); exp.append(e_val); net.append(monthly_net)
            cumulative_data.append(cumulative_flow)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='Total Inflow', x=period_strs, y=inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Total Outflow', x=period_strs, y=exp, marker_color='#ef4444'))
        fig.add_trace(go.Scatter(name='Cumulative Net Flow', x=period_strs, y=cumulative_data, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Total Cash Flow (Liquidity View)', yaxis=dict(title='Flow Volume ($)', tickformat="$,.0f"), barmode='group', xaxis=self.xaxis_date, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=self.margin_legend, shapes=shapes, annotations=anns, **self.base_layout)
        return to_json(fig, pretty=True)
    
    def _chart_core_summary(self, periods, period_strs, grouped_txs, shapes, anns):
        payee_map = {}
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            for t in p_txs:
                if t.category.type == 'Expense' and t.category.name not in EXCLUDED_CAT_CORE:
                    name = t.payee.rule.display_name if t.payee.rule else t.payee.name
                    if name not in EXCLUDED_PAYEE_LABELS_CORE:
                        payee_map[name] = payee_map.get(name, 0.0) + float(t.amount)
        sorted_payees = sorted(payee_map.items(), key=lambda item: item[1])[:20]
        sorted_payees = sorted_payees[::-1] 
        names = [p[0] for p in sorted_payees]
        vals = [abs(p[1]) for p in sorted_payees] 
        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#6366f1', text=vals, texttemplate='$%{x:,.0f}', hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>')])
        fig.update_layout(title=f'Top 20 Core Operating Payees (Current View)', xaxis=dict(title='Total Spent', tickformat="$,.0f"), margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)
    
    def _chart_core_operating(self, periods, period_strs, grouped_txs, shapes, anns):
        c_inc, c_exp, net = [], [], []
        cumulative_surplus = 0.0
        cumulative_net = []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            inc_val = float(sum(t.amount for t in p_txs if t.category.type == 'Income' and t.category.name != 'Empower IRA'))
            exp_val = float(abs(sum(t.amount for t in p_txs if 
                t.category.type == 'Expense' and 
                t.category.name not in EXCLUDED_CAT_CORE and
                (t.payee.rule.display_name if t.payee.rule else t.payee.name) not in EXCLUDED_PAYEE_LABELS_CORE
            )))
            current_surplus = inc_val - exp_val
            cumulative_surplus += current_surplus 
            c_inc.append(inc_val)
            c_exp.append(exp_val)
            cumulative_net.append(cumulative_surplus) 
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Core Income', x=period_strs, y=c_inc, marker_color='#10b981'))
        fig.add_trace(go.Bar(name='Core Expenses', x=period_strs, y=c_exp, marker_color='#6366f1'))
        fig.add_trace(go.Scatter(name='Cumulative Surplus', x=period_strs, y=cumulative_net, mode='lines+markers', line=dict(color='#f59e0b', width=3)))
        fig.update_layout(title='Core Operating Performance', barmode='group', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.3, xanchor="center", x=0.5), margin=self.margin_legend, shapes=shapes, annotations=anns, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_groceries(self, periods, period_strs, grouped_txs, shapes, anns):
        groc, dine = [], []
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            groc.append(float(abs(sum(t.amount for t in p_txs if t.category.name == 'Groceries'))))
            dine.append(float(abs(sum(t.amount for t in p_txs if t.category.name == 'Eat Out'))))
        
        avg_groc = sum(groc) / len(groc) if len(groc) > 0 else 0
        avg_dine = sum(dine) / len(dine) if len(dine) > 0 else 0

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
        payee_totals = {}
        for t in txs:
            # Filter by date and ensure it is an expense
            if t.date >= start_date and t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT:
                name = t.payee.rule.display_name if t.payee.rule else t.payee.name
                payee_totals[name] = payee_totals.get(name, 0) + float(t.amount)
        
        # Sort and take top 20
        sorted_payees = sorted(payee_totals.items(), key=lambda item: item[1])[:20]
        sorted_payees = sorted_payees[::-1] 
        names = [p[0] for p in sorted_payees]
        vals = [abs(p[1]) for p in sorted_payees]
        
        fig = go.Figure(data=[go.Bar(x=vals, y=names, orientation='h', marker_color='#6366f1', text=vals, texttemplate='$%{x:,.0f}', hovertemplate='%{y}<br>$%{x:,.2f}<extra></extra>')])
        fig.update_layout(title=f'Top 20 Payees (Current View)', xaxis=dict(title='Total Spent', tickformat="$,.0f"), margin=self.margin_std, **self.base_layout)
        return to_json(fig, pretty=True)

    def _chart_yoy(self):
        # Comparison of the last 3 years
        years_to_show = [self.today.year - 2, self.today.year - 1, self.today.year]
        # Colors: Oldest (Gray), Previous (Amber), Current (Indigo)
        colors = ['#9ca3af', '#f59e0b', '#6366f1'] 
        
        fig = go.Figure()

        for i, yr in enumerate(years_to_show):
            # 1. Fetch data for this specific year
            yr_txs = [t for t in self.core_transactions if t.date.year == yr]
            
            # 2. Aggregate by month
            monthly_totals = {}
            for t in yr_txs:
                p_name = t.payee.rule.display_name if t.payee.rule else t.payee.name
                
                # PRESERVED: Specific JEA filter from original code
                if 'JEA' in p_name.upper() and t.category.type == 'Expense':
                     m = t.date.month
                     monthly_totals[m] = monthly_totals.get(m, 0.0) + float(t.amount)
            
            # 3. Format for Plotly (1-12 months)
            # Use 0.0 instead of None so the bar shows as flat/empty rather than breaking the chart
            y_vals = []
            for m in range(1, 13):
                val = monthly_totals.get(m, 0.0)
                # If it's the current year and the month hasn't happened yet, use None to hide the bar
                if yr == self.today.year and m > self.today.month:
                    y_vals.append(None)
                else:
                    y_vals.append(abs(val))

            # 4. Add Bar Trace
            fig.add_trace(go.Bar(
                name=str(yr),
                x=list(calendar.month_name[1:]),
                y=y_vals,
                marker_color=colors[i],
                text=y_vals,
                texttemplate='$%{y:,.0f}',
                textposition='auto'
            ))

        fig.update_layout(
            title='YoY JEA Expenses (3-Year Comparison)',
            barmode='group', # This groups the bars side-by-side
            yaxis=dict(title='$', tickformat="$,.0f"),
            xaxis=self.xaxis_cat,
            margin=self.margin_std,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            **self.base_layout
        )
        return to_json(fig, pretty=True)
    
    def _chart_expense_broad(self, periods, period_strs, grouped_txs, shapes, anns):
        cat_data = {}
        all_cats = set()
        for p_str in period_strs:
            p_txs = grouped_txs.get(p_str, [])
            # Only Expenses, not Excluded
            p_txs = [t for t in p_txs if t.category.type == 'Expense' and t.category.name != EXCLUDED_CAT]
            
            if p_str not in cat_data: cat_data[p_str] = {}
            temp_cat_totals = {}
            for t in p_txs:
                c_name = t.category.name
                all_cats.add(c_name)
                temp_cat_totals[c_name] = temp_cat_totals.get(c_name, 0.0) + float(t.amount)
            for c, val in temp_cat_totals.items():
                cat_data[p_str][c] = abs(val) if val < 0 else 0.0
        fig = go.Figure()
        for cat in sorted(list(all_cats), reverse=True):
            y_vals = [cat_data.get(m, {}).get(cat, 0) for m in period_strs]
            fig.add_trace(go.Bar(name=cat, x=period_strs, y=y_vals))
        fig.update_layout(title='Expenses by Category (Net)', barmode='stack', yaxis=dict(title='$', tickformat="$,.0f"), xaxis=self.xaxis_date, shapes=shapes, annotations=anns, margin=self.margin_events, legend=dict(traceorder='reversed'), **self.base_layout)
        return to_json(fig, pretty=True)

# --- AI INSIGHTS LOGIC ---

def get_spending_data_for_period(start, end):
    # Exclude HSA accounts from Insights
    hsa_subquery = db.session.query(Account.id).filter(Account.account_type == 'hsa')
    
    transactions = Transaction.query.join(Category).filter(
        Transaction.date >= start, 
        Transaction.date <= end, 
        Transaction.is_deleted == False,
        Transaction.account_id.notin_(hsa_subquery) # EXCLUDE HSA
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
# --- Helper Function for Buckets ---
def generate_buckets(start_date, end_date, bucket_type):
    # Generates a list of string dates 'YYYY-MM-DD' representing the start of each bucket
    current = start_date
    buckets = []
    
    # Align start_date to the beginning of the bucket
    if bucket_type == 'year':
        current = current.replace(month=1, day=1)
    elif bucket_type == 'month':
        current = current.replace(day=1)
    elif bucket_type == 'week':
        # Align to Monday (Postgres default)
        current = current - timedelta(days=current.weekday())

    while current <= end_date:
        buckets.append(current.strftime('%Y-%m-%d'))
        
        if bucket_type == 'year':
            current = current.replace(year=current.year + 1)
        elif bucket_type == 'month':
            y, m = current.year, current.month
            if m == 12: current = current.replace(year=y+1, month=1)
            else: current = current.replace(month=m+1)
        elif bucket_type == 'week':
            current += timedelta(weeks=1)
            
    return buckets

# Update the Index Route to handle the 'view' parameter
@app.route('/', defaults={'month_offset': '0'})
@app.route('/<string:month_offset>')
def index(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    
    view_mode = request.args.get('view', 'monthly') # Get view param
    
    service = DashboardService(view_mode=view_mode)
    summary = service.get_summary_for_dashboard(month_offset)
    charts = service.generate_all_charts()
    
    return render_template('index.html', month_offset=month_offset, summary=summary, accounts=Account.query.all(), gemini_api_key=os.getenv('GEMINI_API_KEY'), view_mode=view_mode, **charts)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    account_id = request.form.get('account_id')
    files = request.files.getlist('file')
    
    # Capture manual dates if provided (specifically for CSVs)
    manual_start_str = request.form.get('start_date')
    manual_end_str = request.form.get('end_date')
    manual_start = try_parse_date(manual_start_str)
    manual_end = try_parse_date(manual_end_str)

    if not account_id or not files: return redirect(url_for('index'))
    account = db.session.get(Account, account_id)
    if not account: abort(404)
    uncat_id = get_uncategorized_id()
    added = 0
    
    for file in files:
        try:
            df = pd.DataFrame()
            p_start, p_end = None, None

            # 1. Handle CSV (Uses Manual Dates)
            if file.filename.lower().endswith('.csv'):
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                df = pd.read_csv(stream)
                
                # CSV Processing Logic
                if df.shape[1] >= 5:
                    df = df.iloc[:, [0, 1, 4]]
                    df.columns = ['Date', 'Amount', 'Description']
                
                # Use manual dates for CSVs if provided
                if manual_start and manual_end:
                    p_start, p_end = manual_start, manual_end
                
            # 2. Handle PDF (Attempts Parsing, falls back to Manual)
            elif file.filename.lower().endswith('.pdf'):
                data = None
                if account.account_type == 'hsa':
                    data, parsed_start, parsed_end = parse_hsa_pdf(file.stream)
                elif account.account_type in ['checking', 'savings']:
                    data, parsed_start, parsed_end = parse_wellsfargo_pdf(file.stream)
                else:
                    data, parsed_start, parsed_end = parse_chase_pdf(file.stream)
                
                # Use parsed dates, override with manual if parsed failed but manual exists
                p_start = parsed_start if parsed_start else manual_start
                p_end = parsed_end if parsed_end else manual_end

                if data: df = pd.DataFrame(data)
            
            # 3. Record Statement Period (Logic Check)
            if p_start and p_end:
                # Check for exact duplicate period
                existing = StatementRecord.query.filter_by(account_id=account.id, start_date=p_start, end_date=p_end).first()
                if existing:
                    flash(f"Skipped {file.filename}: Statement period {p_start} - {p_end} already recorded.", "warning")
                    # We continue to process transactions even if period exists, 
                    # or you can 'continue' here to skip file entirely if that's preferred.
                    # Per requirement "display what we have", preventing duplicate tracking is key.
                else:
                    db.session.add(StatementRecord(account_id=account.id, start_date=p_start, end_date=p_end))
                    db.session.commit()
            elif file.filename.lower().endswith('.csv') and (not manual_start or not manual_end):
                 flash(f"Warning: CSV uploaded without date range. Statement history not updated for {file.filename}.", "warning")

            if df.empty: continue

            # 4. Transaction Processing (Existing Logic)
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
                    
                    new_transactions.append(Transaction(date=date_val, original_description=desc, amount=amount, payee_id=payee.id, category_id=cat_id, account_id=account.id))
                    added += 1
                except Exception: continue
            
            if new_transactions:
                db.session.add_all(new_transactions)
                db.session.commit()
                
        except Exception as e:
            db.session.rollback()
            flash(f"File Error {file.filename}: {e}", "danger")
            
    if added > 0: flash(f"Imported {added} transactions.", "success")
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

@app.route('/statement_history')
def statement_history():
    # 1. Get Filter Parameters
    page = request.args.get('page', 1, type=int)
    account_filter = request.args.get('account_id', type=int)
    year_filter = request.args.get('year', type=int)
    per_page = 20

    # 2. Build Query
    query = db.session.query(StatementRecord).join(Account).order_by(StatementRecord.start_date.desc())

    if account_filter:
        query = query.filter(StatementRecord.account_id == account_filter)
    
    if year_filter:
        # Filter by the start_date's year
        query = query.filter(extract('year', StatementRecord.start_date) == year_filter)

    # 3. Paginate
    total_records = query.count()
    # Manual slicing for pagination logic consistent with app style
    start = (page - 1) * per_page
    end = start + per_page
    records = query.slice(start, end).all()
    
    # 4. Filter Options Data
    accounts = Account.query.order_by(Account.name).all()
    
    # Get distinct years from StatementRecords for the dropdown
    available_years_query = db.session.query(extract('year', StatementRecord.start_date)).distinct().order_by(extract('year', StatementRecord.start_date).desc()).all()
    available_years = [int(y[0]) for y in available_years_query]

    # Pagination controls
    has_next = total_records > end
    has_prev = page > 1
    total_pages = (total_records + per_page - 1) // per_page

    return render_template(
        'statement_history.html', 
        records=records, 
        accounts=accounts, 
        available_years=available_years,
        current_account=account_filter,
        current_year=year_filter,
        page=page,
        has_next=has_next,
        has_prev=has_prev,
        total_pages=total_pages
    )

@app.route('/delete_statement_record/<int:record_id>', methods=['POST'])
def delete_statement_record(record_id):
    record = db.session.get(StatementRecord, record_id)
    if record:
        db.session.delete(record)
        db.session.commit()
        flash("Statement period record deleted (Transactions remain).", "success")
    return redirect(url_for('statement_history'))

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
    # This function (defined on line 147) scans all payees for each rule
    for r in rules: total += run_rule_on_all_payees(r, overwrite=True)
    
    flash(f"Ran {len(rules)} rules. Updated {total} payees.", "success")
    
    # NEW: Check if a 'next' URL was passed (e.g., from Manage Payees page)
    next_page = request.args.get('next')
    if next_page:
        return redirect(next_page)
        
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
        # UPDATED QUERY: Outer join PayeeRule and add search condition for display_name
        query = Transaction.query.join(Payee).outerjoin(PayeeRule).filter(
            or_(
                Payee.name.ilike(f"%{srch}%"),
                Transaction.original_description.ilike(f"%{srch}%"),
                PayeeRule.display_name.ilike(f"%{srch}%")
            )
        )
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

@app.route('/monthly_averages')
def monthly_averages():
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped_categories = {}
    for c in cats:
        if c.type not in grouped_categories: grouped_categories[c.type] = []
        grouped_categories[c.type].append(c)
    
    accounts = Account.query.order_by(Account.name).all()
    
    # Get Payees mapped to Categories
    # Structure: { category_id: [list of payee names] }
    payee_map = {}
    
    # Query PayeeRules joined with Categories
    # This covers all "Rules-based" payees
    rules = db.session.query(PayeeRule.category_id, PayeeRule.display_name).order_by(PayeeRule.display_name).all()
    for r in rules:
        if r.category_id not in payee_map: payee_map[r.category_id] = []
        if r.display_name not in payee_map[r.category_id]:
            payee_map[r.category_id].append(r.display_name)
            
    # Also fetch distinct payees (labels) for the generic list
    results = db.session.query(func.coalesce(PayeeRule.display_name, Payee.name).label('label')).select_from(Payee).outerjoin(PayeeRule).distinct().order_by('label').all()
    unique_labels = [r.label for r in results if r.label]
    
    budgets = Budget.query.order_by(Budget.name).all()
    
    return render_template('monthly_averages.html', grouped_categories=grouped_categories, payee_labels=unique_labels, accounts=accounts, budgets=budgets, category_payees=payee_map)

@app.route('/api/average_data', methods=['POST'])
def get_average_data():
    data = request.get_json()
    cat_ids = data.get('category_ids', [])
    payee_names = data.get('payee_names', [])
    acct_ids = [int(i) for i in data.get('account_ids', [])]
    excluded_payees = data.get('excluded_payees', [])
    
    try:
        s_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
        e_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format'}), 400

    num_months = (e_date.year - s_date.year) * 12 + (e_date.month - s_date.month) + 1
    if num_months < 1: num_months = 1

    results = []

    # 1. Calculate Totals for Selected Categories
    if cat_ids:
        q = db.session.query(Category.id, Category.name, func.sum(Transaction.amount).label('total'))\
            .join(Category)
            
        # For the MAIN TOTAL, we DO apply the exclusion filter
        if payee_names:
            q = q.join(Payee).outerjoin(PayeeRule)
            name_col = func.coalesce(PayeeRule.display_name, Payee.name)
            q = q.filter(name_col.notin_(payee_names))
            
        if excluded_payees:
            if not payee_names: q = q.join(Payee).outerjoin(PayeeRule)
            name_col = func.coalesce(PayeeRule.display_name, Payee.name)
            q = q.filter(name_col.notin_(excluded_payees))

        q = q.filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False)\
            .filter(Transaction.category_id.in_(cat_ids))
        
        if acct_ids:
            q = q.filter(Transaction.account_id.in_(acct_ids))
            
        q = q.group_by(Category.id, Category.name)
        
        for row in q.all():
            total = abs(float(row.total)) if row.total else 0.0
            
            # Sub-query for Payee Breakdown: DO NOT filter excluded_payees here!
            # We want them in the list so they can be rendered (unchecked)
            payee_q = db.session.query(
                func.coalesce(PayeeRule.display_name, Payee.name).label('payee_name'),
                func.sum(Transaction.amount).label('ptotal')
            ).select_from(Transaction).join(Payee).outerjoin(PayeeRule)\
             .filter(Transaction.category_id == row.id) \
             .filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False)

            if acct_ids:
                payee_q = payee_q.filter(Transaction.account_id.in_(acct_ids))

            if payee_names:
                 name_col_sub = func.coalesce(PayeeRule.display_name, Payee.name)
                 payee_q = payee_q.filter(name_col_sub.notin_(payee_names))
            
            # IMPORTANT: We DO NOT apply excluded_payees filter here.
            # This allows the frontend to receive the data for excluded items so it can show them as unchecked rows.

            payee_q = payee_q.group_by(func.coalesce(PayeeRule.display_name, Payee.name))
            
            sub_payees = []
            for p_row in payee_q.all():
                p_total = abs(float(p_row.ptotal))
                sub_payees.append({
                    'name': p_row.payee_name,
                    'total': p_total,
                    'average': p_total / num_months
                })
            
            sub_payees.sort(key=lambda x: x['total'], reverse=True)

            results.append({
                'name': row.name,
                'type': 'Category',
                'total': total,
                'average': total / num_months,
                'breakdown': sub_payees 
            })

    # 2. Calculate Totals for Selected Payees
    if payee_names:
        name_col = func.coalesce(PayeeRule.display_name, Payee.name)
        q = db.session.query(name_col.label('name'), func.sum(Transaction.amount).label('total'))\
            .select_from(Transaction).join(Payee).outerjoin(PayeeRule)\
            .filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False)\
            .filter(name_col.in_(payee_names))
            
        if acct_ids:
            q = q.filter(Transaction.account_id.in_(acct_ids))
            
        q = q.group_by(name_col)
        
        for row in q.all():
            total = abs(float(row.total)) if row.total else 0.0
            results.append({
                'name': row.name,
                'type': 'Payee',
                'total': total,
                'average': total / num_months,
                'breakdown': [] 
            })

    results.sort(key=lambda x: x['total'], reverse=True)

    return jsonify({'results': results, 'num_months': num_months})

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

@app.route('/api/save_budget', methods=['POST'])
def save_budget():
    try:
        data = request.get_json()
        name = data.get('name')
        if not name: return jsonify({'success': False, 'message': 'Budget name required'})
        
        s_date = try_parse_date(data.get('start_date'))
        e_date = try_parse_date(data.get('end_date'))

        criteria = json.dumps({
            'category_ids': data.get('category_ids', []),
            'payee_names': data.get('payee_names', []),
            'account_ids': data.get('account_ids', []),
            'excluded_payees': data.get('excluded_payees', []) # Save exclusions
        })
        
        budget = Budget.query.filter_by(name=name).first()
        if budget:
            budget.criteria = criteria
            budget.start_date = s_date
            budget.end_date = e_date
            msg = 'Budget updated'
        else:
            budget = Budget(name=name, criteria=criteria, start_date=s_date, end_date=e_date)
            db.session.add(budget)
            msg = 'Budget saved'
        
        db.session.commit()
        return jsonify({'success': True, 'message': msg, 'id': budget.id})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/load_budget/<int:budget_id>')
def load_budget(budget_id):
    budget = db.session.get(Budget, budget_id)
    if not budget: return jsonify({'success': False})
    
    criteria = json.loads(budget.criteria)
    criteria['start_date'] = budget.start_date.strftime('%Y-%m-%d') if budget.start_date else ''
    criteria['end_date'] = budget.end_date.strftime('%Y-%m-%d') if budget.end_date else ''
    
    return jsonify({'success': True, 'name': budget.name, 'criteria': criteria})

@app.route('/api/delete_budget/<int:budget_id>', methods=['POST'])
def delete_budget(budget_id):
    Budget.query.filter_by(id=budget_id).delete()
    db.session.commit()
    return jsonify({'success': True})
    return jsonify({'success': True})

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
    
    accounts = Account.query.order_by(Account.name).all()
    
    results = db.session.query(func.coalesce(PayeeRule.display_name, Payee.name).label('label')).select_from(Payee).outerjoin(PayeeRule).distinct().order_by('label').all()
    unique_labels = [r.label for r in results if r.label]
    
    # CHANGED: Added EXCLUDED_CAT_CORE and EXCLUDED_CAT to the context
    return render_template('trends.html', 
                           grouped_categories=grouped_categories, 
                           all_events=Event.query.all(), 
                           payee_labels=unique_labels, 
                           accounts=accounts,
                           excluded_core=EXCLUDED_CAT_CORE,
                           excluded_cat=EXCLUDED_CAT)

@app.route('/api/trend_data', methods=['POST'])
def get_trend_data():
    data = request.get_json()
    cat_ids = data.get('category_ids', [])
    payee_names = data.get('payee_names', [])
    evt_ids = [int(i) for i in data.get('event_ids', [])]
    
    # FIX: Retrieve account_ids from request
    acct_ids = [int(i) for i in data.get('account_ids', [])] 
    
    s_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
    e_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
    bucket = data.get('time_bucket', 'month')
    chart_type = 'scatter' if data.get('chart_type') in ['line','area'] else 'bar'
    chart_mode = 'lines+markers' if data.get('chart_type') == 'line' else None
    fill_type = 'tozeroy' if data.get('chart_type') == 'area' else None

    data_by_item = {}
    sorted_buckets = generate_buckets(s_date, e_date, bucket)
    
    if cat_ids:
        bucket_col = func.date_trunc(bucket, Transaction.date)
        query = db.session.query(bucket_col.label('bucket_start'), Category.name.label('name'), func.sum(Transaction.amount).label('total')).select_from(Transaction).join(Category).filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False, Transaction.category_id.in_(cat_ids))
        
        # FIX: Apply Account ID filter
        if acct_ids:
            query = query.filter(Transaction.account_id.in_(acct_ids))
            
        query = query.group_by(bucket_col, Category.name).order_by(bucket_col)
        for row in query.all():
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            if row.name not in data_by_item: data_by_item[row.name] = {}
            data_by_item[row.name][b_date] = data_by_item[row.name].get(b_date, 0) + abs(float(row.total))

    if payee_names:
        bucket_col = func.date_trunc(bucket, Transaction.date)
        name_col = func.coalesce(PayeeRule.display_name, Payee.name)
        query = db.session.query(bucket_col.label('bucket_start'), name_col.label('name'), func.sum(Transaction.amount).label('total')).select_from(Transaction).join(Payee).outerjoin(PayeeRule).filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False, name_col.in_(payee_names))
        
        # FIX: Apply Account ID filter
        if acct_ids:
            query = query.filter(Transaction.account_id.in_(acct_ids))
            
        query = query.group_by(bucket_col, name_col).order_by(bucket_col)
        for row in query.all():
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            name = f"[P] {row.name}"
            if name not in data_by_item: data_by_item[name] = {}
            data_by_item[name][b_date] = data_by_item[name].get(b_date, 0) + abs(float(row.total))

    plot_data = []
    for name, pts in data_by_item.items():
        y_vals = [pts.get(b, 0) for b in sorted_buckets]
        plot_data.append({'type': chart_type, 'name': name, 'x': sorted_buckets, 'y': y_vals, 'mode': chart_mode, 'fill': fill_type})
        # Avg line logic (omitted for brevity in this snippet, assumes present in full code)

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
    app.run(host='0.0.0.0', port=5000, debug=True)