# Import necessary libraries
import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, cast, Date
import pandas as pd
import plotly.graph_objects as go
from plotly.io import to_json 
import io
import json
import calendar
import re
from datetime import datetime, date, timedelta 
import pdfplumber 

# --- Configuration ---
app = Flask(__name__)
# IMPORTANT: Update this line with your actual PostgreSQL connection string
# FORMAT: 'postgresql://USER:PASSWORD@HOST:PORT/DATABASE_NAME'
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://budget_user:ben@localhost:5432/budget_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24) # Required for flashing messages

db = SQLAlchemy(app)

# --- Models ---
class Account(db.Model):
    """Stores user accounts (Checking, Savings, Credit Card)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    account_type = db.Column(db.String(50), nullable=False) # 'checking', 'savings', 'credit_card', 'cash', 'loan', 'investment'
    starting_balance = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)
    transactions = db.relationship('Transaction', backref='account', lazy=True, cascade="all, delete-orphan")

class Category(db.Model):
    """Stores categories for budgeting (e.g., Groceries, Rent, Salary)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False) # Removed unique=True
    category_group = db.Column(db.String(80), nullable=False) # e.g., 'Income', 'Needs', 'Wants'
    transactions = db.relationship('Transaction', backref='category', lazy=True)

class CategoryRule(db.Model):
    """Stores keyword rules for auto-categorization."""
    id = db.Column(db.Integer, primary_key=True)
    fragment = db.Column(db.String(100), nullable=False) # The text to match (e.g., "STARBUCKS")
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    
    # Relationship to fetch the category name easily
    category = db.relationship('Category', backref='rules')

class Transaction(db.Model):
    """Stores individual financial transactions."""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

class Event(db.Model):
    """Stores significant dates (e.g., 'Moved House', 'Started Diet') for graph annotation."""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(100), nullable=False)

# --- Database Helper Functions (for setup) ---
def create_tables():
    """Initializes the database schema and seeds initial data if needed."""
    db.create_all()
    if Category.query.count() == 0:
        # Seed initial categories
        categories_data = [
            {'name': 'Other', 'category_group': 'Uncategorized'}, # ID 1
            {'name': 'Salary', 'category_group': 'Income'},
            {'name': 'Rental', 'category_group': 'Income'},
            {'name': 'Dividend', 'category_group': 'Income'},
            {'name': 'Venmo', 'category_group': 'Income'},
            {'name': 'Interest', 'category_group': 'Income'},
            {'name': 'Groceries', 'category_group': 'Expense'},
            {'name': 'Venmo', 'category_group': 'Expense'},
            {'name': 'Raney Gerber Life INS', 'category_group': 'Expense'},
            {'name': 'Mortgage', 'category_group': 'Expense'},
            {'name': 'Eat Out', 'category_group': 'Expense'},
            {'name': 'Church Donation', 'category_group': 'Expense'},
            {'name': 'JEA', 'category_group': 'Expense'},
            {'name': 'Credit Card BJ Payment', 'category_group': 'Expense'},
            {'name': 'Pool', 'category_group': 'Expense'},
            {'name': 'Tmobile', 'category_group': 'Expense'},
            {'name': 'Fidelity Transfer', 'category_group': 'Transfers'},
            {'name': 'Ben Fidelity Transfer', 'category_group': 'Transfers'},
            {'name': 'Savings Transfer', 'category_group': 'Transfers'},
            {'name': 'Credit Card Chase Payment', 'category_group': 'Transfers'},
        ]
        for data in categories_data:
            db.session.add(Category(**data))
        db.session.commit()

# --- PDF Parsing Function ---
def parse_chase_pdf(file_stream):
    """
    Parses a Chase credit card PDF statement.
    Returns a list of transaction dicts or None if parsing fails.
    """
    transactions = []
    end_year, end_month = None, None

    date_regex = re.compile(r"Opening/Closing Date\s*([\d/]+)\s*-\s*([\d/]+)")
    trans_regex = re.compile(r"(\d{1,2}/\d{1,2})\s+(.+)\s+([\d,.-]+\.\d{2})")

    try:
        with pdfplumber.open(file_stream) as pdf:
            page1_text = pdf.pages[0].extract_text()
            date_match = date_regex.search(page1_text)
            if not date_match:
                print("PDF Parse Error: Could not find 'Opening/Closing Date' on Page 1.")
                return None
                
            end_date_str = date_match.group(2)
            end_date = datetime.strptime(end_date_str, '%m/%d/%y').date()
            end_year = end_date.year
            end_month = end_date.month

            full_text = ""
            for page in pdf.pages:
                full_text += page.extract_text() + "\n"

            matches = trans_regex.findall(full_text)
            
            for match in matches:
                trans_date_str, description, amount_str = match
                
                description = re.sub(r'\s+', ' ', description).strip()

                if "Order Number" in description:
                    continue
                
                trans_month = int(trans_date_str.split('/')[0])
                year = end_year
                if trans_month > end_month:
                    year = end_year - 1
                    
                full_date_str = f"{trans_month}/{trans_date_str.split('/')[1]}/{year}"
                trans_date = datetime.strptime(full_date_str, '%m/%d/%Y').date()
                
                try:
                    amount = float(amount_str.replace(',', ''))
                except ValueError:
                    print(f"PDF Parse Warning: Skipping line. Could not convert amount to float: '{amount_str}' (Desc: '{description}')")
                    continue 

                transactions.append({
                    'Date': trans_date,
                    'Description': description,
                    'Amount': amount
                })

        return transactions

    except Exception as e:
        print(f"Error parsing PDF: {e}")
        return None

# --- Utility Functions ---

def get_monthly_summary(month_offset):
    today = date.today()
    target_month_index = today.month - 1 + month_offset
    year = today.year + (target_month_index // 12)
    month = (target_month_index % 12) + 1
    
    start_date = date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    end_date = date(year, month, last_day)
    
    monthly_transactions = Transaction.query.filter(
        Transaction.date >= start_date,
        Transaction.date <= end_date
    ).order_by(Transaction.date.desc()).all()
    
    total_income = sum(t.amount for t in monthly_transactions if t.amount > 0 and not t.is_deleted)
    total_expense = sum(t.amount for t in monthly_transactions if t.amount < 0 and not t.is_deleted) * -1
    
    category_spending = db.session.query(
        Category.name, 
        func.sum(Transaction.amount).label('total')
    ).join(Transaction).filter(
        Transaction.date >= start_date,
        Transaction.date <= end_date,
        Transaction.amount < 0,
        Transaction.is_deleted == False
    ).group_by(Category.name).all()
    
    net_worth = 0.0
    account_balances = {}
    all_accounts = Account.query.all()
    
    for account in all_accounts:
        net_change_query = db.session.query(
            func.sum(Transaction.amount)
        ).filter(
            Transaction.account_id == account.id,
            Transaction.date <= today,
            Transaction.is_deleted == False
        ).scalar()
        
        net_change = float(net_change_query) if net_change_query is not None else 0.0
        current_balance = float(account.starting_balance) + net_change
        account_balances[account.name] = current_balance
        net_worth += current_balance

    uncategorized_count = Transaction.query.filter_by(category_id=1, is_deleted=False).count()

    return {
        'start_date': start_date,
        'end_date': end_date,
        'current_month_name': start_date.strftime('%B %Y'),
        'total_income': total_income,
        'total_expense': total_expense,
        'category_spending': category_spending,
        'net_worth': net_worth,
        'account_balances': account_balances,
        'uncategorized_count': uncategorized_count,
    }


# --- REFACTORED: Helper Function for Insights ---
def get_spending_data_for_period(start, end):
    """Helper to get expense totals and net income for a period."""
    if not start or not end:
        return {}, 0

    transactions = Transaction.query.join(Category).filter(
        Transaction.date >= start,
        Transaction.date <= end,
        Transaction.is_deleted == False
    ).all()
    
    spending_by_cat = {}
    total_income = 0
    total_expense = 0

    for t in transactions:
        if t.category.category_group == 'Expense':
            total = spending_by_cat.get(t.category.name, 0)
            spending_by_cat[t.category.name] = total + abs(float(t.amount))
        elif t.category.category_group == 'Income':
            total_income += float(t.amount)
    
    # Round the values for cleaner JSON
    for cat, total in spending_by_cat.items():
        spending_by_cat[cat] = round(total, 2)
        
    total_expense = sum(spending_by_cat.values())
    net_income = round(total_income - total_expense, 2)
    
    return spending_by_cat, net_income
# --- End of Refactored Helper ---


# --- UPDATED: Monthly Insight Function ---
def get_insight_data(today=None):
    """Fetches spending data for the LAST FULL month vs. the PRIOR FULL month."""
    if today is None:
        today = date.today() # e.g., Nov 17, 2025

    # 1. Get Last Full Month (October)
    last_month_end = today.replace(day=1) - timedelta(days=1) # Oct 31, 2025
    last_month_start = last_month_end.replace(day=1) # Oct 1, 2025

    # 2. Get Prior Full Month (September)
    prior_month_end = last_month_start - timedelta(days=1) # Sep 30, 2025
    prior_month_start = prior_month_end.replace(day=1) # Sep 1, 2025

    # 3. Use the refactored helper
    last_month_spending, last_month_net = get_spending_data_for_period(last_month_start, last_month_end)
    prior_month_spending, prior_month_net = get_spending_data_for_period(prior_month_start, prior_month_end)
    
    return {
        "last_month_name": last_month_start.strftime('%B %Y'),
        "last_month_spending": last_month_spending,
        "prior_month_spending": prior_month_spending,
        "last_month_net": last_month_net,
        "prior_month_net": prior_month_net
    }
# --- End of Updated Function ---

# --- Yearly Insight Function ---
def get_yearly_insight_data(today=None):
    """Fetches YTD spending data vs. previous YTD for AI analysis."""
    if today is None:
        today = date.today()

    # 1. Get Current YTD Data (Jan 1, 2025 -> Nov 17, 2025)
    start_date_current_ytd = today.replace(day=1, month=1)
    end_date_current_ytd = today

    # 2. Get Last YTD Data (Jan 1, 2024 -> Nov 17, 2024)
    end_date_last_ytd = today.replace(year=today.year - 1)
    start_date_last_ytd = end_date_last_ytd.replace(day=1, month=1)

    # 3. Use the refactored helper
    current_ytd_spending, current_ytd_net = get_spending_data_for_period(start_date_current_ytd, end_date_current_ytd)
    last_ytd_spending, last_ytd_net = get_spending_data_for_period(start_date_last_ytd, end_date_last_ytd)
    
    return {
        "current_ytd_spending": current_ytd_spending,
        "last_ytd_spending": last_ytd_spending,
        "current_ytd_net": current_ytd_net,
        "last_ytd_net": last_ytd_net,
        "current_year": today.year,
        "last_year": today.year - 1
    }
# --- End of Yearly Function ---


def create_plot(plot_type, data):
    """Generates Plotly JSON for dashboard charts."""
    
    layout_settings = dict(
        paper_bgcolor='rgba(0,0,0,0)', 
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#71717a'), 
        margin={'t': 50, 'b': 20, 'l': 50, 'r': 10},
        autosize=True
    )

    if plot_type == 'income_vs_expense':
        months_labels = []
        incomes = []
        expenses = []
        today = date.today()
        
        for i in range(11, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            while month > 12:
                month -= 12
                year += 1
            _, last_day = calendar.monthrange(year, month)
            month_start = date(year, month, 1)
            month_end = date(year, month, last_day)
            
            monthly_txs = Transaction.query.join(Category).filter(
                Transaction.date >= month_start,
                Transaction.date <= month_end,
                Transaction.is_deleted == False 
            ).all()
            
            inc = sum(t.amount for t in monthly_txs if t.category.category_group == 'Income')
            exp = sum(t.amount for t in monthly_txs if t.category.category_group == 'Expense') * -1 
            
            months_labels.append(month_start.strftime('%b %Y'))
            incomes.append(inc)
            expenses.append(exp)

        fig = go.Figure(data=[
            go.Bar(name='Income', x=months_labels, y=incomes, marker_color='rgb(34, 197, 94)'),
            go.Bar(name='Expense', x=months_labels, y=expenses, marker_color='rgb(239, 68, 68)')
        ])
        fig.update_layout(
            title_text='Income vs. Expense (Operating)',
            yaxis_title='Amount ($)',
            barmode='group',
            yaxis=dict(tickformat="$,.0f"),
            xaxis=dict(rangeslider=dict(visible=True), type="category"),
            **layout_settings
        )
        fig.update_traces(hovertemplate='%{y:$,.2f}')
        return to_json(fig, pretty=True)

    elif plot_type == 'expense_pie':
        today = date.today()
        months_labels = []
        all_categories = [c.name for c in Category.query.filter_by(category_group='Expense').all()]
        category_series = {name: [] for name in all_categories}
        
        for i in range(11, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            while month > 12:
                month -= 12
                year += 1
            _, last_day = calendar.monthrange(year, month)
            month_start = date(year, month, 1)
            month_end = date(year, month, last_day)
            months_labels.append(month_start.strftime('%b %Y'))
            
            monthly_spending = db.session.query(
                Category.name, 
                func.sum(Transaction.amount)
            ).join(Transaction).filter(
                Transaction.date >= month_start,
                Transaction.date <= month_end,
                Category.category_group == 'Expense',
                Transaction.is_deleted == False
            ).group_by(Category.name).all()
            
            month_data = {name: abs(float(amount)) for name, amount in monthly_spending}
            for cat_name in all_categories:
                val = month_data.get(cat_name, 0.0)
                category_series[cat_name].append(val)

        traces = []
        for cat_name, values in category_series.items():
            if sum(values) > 0:
                traces.append(go.Bar(name=cat_name, x=months_labels, y=values))

        fig = go.Figure(data=traces)
        fig.update_layout(
            title_text='Expense Trends (No Transfers)',
            barmode='stack',
            yaxis_title='Amount ($)',
            yaxis=dict(tickformat="$,.0f"),
            xaxis=dict(rangeslider=dict(visible=True), type="category"),
            **layout_settings
        )
        fig.update_traces(hovertemplate='%{y:$,.2f}')
        return to_json(fig, pretty=True)

    elif plot_type == 'top_expenses':
        today = date.today()
        year = today.year
        month = today.month - 11
        while month <= 0:
            month += 12
            year -= 1
        month_start = date(year, month, 1)
        
        top_categories = db.session.query(
            Category.name,
            func.sum(Transaction.amount).label('total')
        ).join(Transaction).filter(
            Transaction.date >= month_start,
            Category.category_group == 'Expense',
            Transaction.is_deleted == False
        ).group_by(Category.name).order_by(func.sum(Transaction.amount).asc()).limit(10).all()
        
        cat_names = [r[0] for r in top_categories]
        cat_values = [abs(float(r[1])) for r in top_categories]
        
        fig = go.Figure(data=[
            go.Bar(
                x=cat_values,
                y=cat_names,
                orientation='h',
                marker=dict(color='rgb(99, 102, 241)'),
                text=cat_values,
                texttemplate='$%{x:,.0f}',
                textposition='auto'
            )
        ])
        fig.update_layout(
            title_text='Top 10 Expense Categories (Last 12 Mo)',
            xaxis_title='Total Spent ($)',
            yaxis=dict(showgrid=False), 
            xaxis=dict(showgrid=True, gridcolor='rgba(200,200,200,0.2)'),
            **layout_settings
        )
        fig.update_traces(hovertemplate='%{x:$,.2f}')
        return to_json(fig, pretty=True)
    
    elif plot_type == 'yoy_comparison':
        today = date.today()
        current_year = today.year
        last_year = current_year - 1
        
        def get_monthly_expenses(year):
            expenses = db.session.query(
                func.extract('month', Transaction.date).label('month'),
                func.sum(Transaction.amount).label('total')
            ).join(Category).filter(
                func.extract('year', Transaction.date) == year,
                Category.category_group == 'Expense',
                Transaction.is_deleted == False
            ).group_by('month').all()
            data_map = {int(m): float(t) * -1 for m, t in expenses} 
            return [data_map.get(m, 0.0) for m in range(1, 13)]

        raw_current = get_monthly_expenses(current_year)
        raw_last = get_monthly_expenses(last_year)
        
        final_current = []
        final_last = []
        for i in range(12):
            val_curr = raw_current[i]
            val_last = raw_last[i]
            if val_curr > 0 and val_last > 0:
                final_current.append(val_curr)
                final_last.append(val_last)
            else:
                final_current.append(None)
                final_last.append(None)

        months = calendar.month_name[1:] 

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=months, y=final_last, mode='lines+markers', name=f'{last_year}', 
            connectgaps=False, line=dict(color='rgb(156, 163, 175)', dash='dash')
        ))
        fig.add_trace(go.Scatter(
            x=months, y=final_current, mode='lines+markers', name=f'{current_year}', 
            connectgaps=False, line=dict(color='rgb(79, 70, 229)', width=3)
        ))
        fig.update_layout(
            title_text='YoY Expenses (Intersecting Months Only)',
            yaxis_title='Expenses ($)',
            yaxis=dict(tickformat="$,.0f"), 
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            **layout_settings
        )
        fig.update_traces(hovertemplate='%{y:$,.2f}')
        return to_json(fig, pretty=True)

    return None


# --- Route Definitions ---
@app.route('/add_category', methods=['POST'])
def add_category():
    """Handles adding a new budgeting category with custom Group support."""
    try:
        new_name = request.form.get('category_name', '').strip()
        new_group = request.form.get('category_group', '').strip().title() 

        if not new_name or not new_group:
            flash('Category Name and Group are required.', 'danger')
            return redirect(url_for('categorize'))
        
        if Category.query.filter_by(name=new_name, category_group=new_group).first():
            flash(f'Category "{new_name}" in group "{new_group}" already exists.', 'warning')
            return redirect(url_for('categorize'))
            
        new_category = Category(
            name=new_name,
            category_group=new_group
        )
        
        db.session.add(new_category)
        db.session.commit()
        flash(f'New category "{new_name}" added to group "{new_group}".', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {e}', 'danger')
        
    return redirect(url_for('categorize'))

@app.route('/edit_accounts', methods=['GET'])
def edit_accounts():
    """Renders the account editing page with current balance calculations."""
    accounts = Account.query.order_by(Account.name).all()
    account_types = ['checking', 'savings', 'credit_card']
    
    for acc in accounts:
        net_change_query = db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.account_id == acc.id,
            Transaction.is_deleted == False # Exclude deleted from balance
        ).scalar()
        
        net_change = float(net_change_query) if net_change_query is not None else 0.0
        acc.current_balance = float(acc.starting_balance) + net_change

    return render_template('edit_account_balances.html', accounts=accounts, account_types=account_types)

@app.route('/update_account', methods=['POST'])
def update_account():
    """Handles updating account name and starting balance."""
    try:
        account_id = request.form.get('account_id')
        if not account_id:
            flash('Account ID missing from form.', 'danger')
            return redirect(url_for('edit_accounts'))
            
        account = Account.query.get_or_404(account_id)
        
        new_name = request.form['name'].strip()
        new_type = request.form['account_type'].strip()
        new_starting_balance = request.form['starting_balance'].strip()
        
        if not new_name or not new_starting_balance:
            flash('Account Name and Starting Balance are required.', 'danger')
            return redirect(url_for('edit_accounts'))
        
        account.name = new_name
        account.account_type = new_type
        account.starting_balance = float(new_starting_balance.replace('$', '').replace(',', '').strip())
        
        db.session.commit()
        flash(f'Account "{account.name}" updated successfully.', 'success')
        
    except ValueError:
        db.session.rollback()
        flash('Invalid balance format. Please enter a valid number.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {e}', 'danger')
        
    return redirect(url_for('edit_accounts'))

@app.route('/delete_account/<int:account_id>', methods=['POST'])
def delete_account(account_id):
    """Handles deleting an account and all its transactions."""
    try:
        account = Account.query.get_or_404(account_id)
        account_name = account.name
        db.session.delete(account)
        db.session.commit()
        flash(f'Account "{account_name}" and all its transactions have been deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred while deleting the account: {e}', 'danger')
    return redirect(url_for('edit_accounts'))


@app.route('/add_account', methods=['POST'])
def add_account():
    """Handles adding a new account."""
    try:
        new_name = request.form.get('account_name', '').strip()
        new_type = request.form.get('account_type', '').strip()
        raw_balance = request.form.get('starting_balance', '0').strip()
        
        if not new_name:
            flash('Account Name is required.', 'danger')
            return redirect(url_for('index'))

        if Account.query.filter_by(name=new_name, account_type=new_type).first():
            flash(f'An account named "{new_name}" with type "{new_type}" already exists.', 'warning')
            return redirect(url_for('index'))
            
        clean_balance = float(str(raw_balance).replace('$', '').replace(',', '').strip())

        new_account = Account(
            name=new_name,
            account_type=new_type,
            starting_balance=clean_balance
        )
        
        db.session.add(new_account)
        db.session.commit()
        flash(f'New account "{new_name}" ({new_type}) created successfully.', 'success')
        
    except ValueError:
        db.session.rollback()
        flash('Invalid balance format.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {e}', 'danger')
        
    return redirect(url_for('index'))


@app.route('/calculate_starting_balance/<int:account_id>', methods=['POST'])
def calculate_starting_balance(account_id):
    """Calculates the historical starting balance."""
    try:
        account = Account.query.get_or_404(account_id)
        data = request.get_json()
        current_actual_balance = float(data.get('current_actual_balance', 0.0))
        
        net_change_query = db.session.query(
            func.sum(Transaction.amount)
        ).filter(
            Transaction.account_id == account_id,
            Transaction.is_deleted == False 
        ).scalar()
        
        total_net_change = float(net_change_query) if net_change_query is not None else 0.0
        required_starting_balance = current_actual_balance - total_net_change
        
        account.starting_balance = required_starting_balance
        db.session.commit()
        
        message = (f"Successfully calculated and set historical starting balance for "
                   f"'{account.name}' to ${required_starting_balance:,.2f}. "
                   f"(Current known balance: ${current_actual_balance:,.2f} - Total transaction change: ${total_net_change:,.2f})")
        
        flash(message, 'success')
        return jsonify({'success': True, 'message': message, 'new_starting_balance': required_starting_balance})

    except ValueError:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Invalid balance format.'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Database error: {e}'}), 500

# --- MODIFIED: UPLOAD ROUTE (HANDLES CSV AND PDF) ---
@app.route('/upload_file', methods=['POST'])
def upload_file():
    """Handles bulk file upload (CSV or PDF) and routes to the correct parser."""
    account_id = request.form.get('account_id')
    if not account_id:
        flash('No account was selected for the upload.', 'danger')
        return redirect(url_for('index'))

    account = Account.query.get_or_404(account_id)
    
    files = request.files.getlist('file')

    if not files or all(f.filename == '' for f in files):
        flash('No files selected for uploading.', 'danger')
        return redirect(url_for('index'))

    total_imported_count = 0
    total_files_processed = 0
    files_with_errors = []

    all_rules = CategoryRule.query.options(db.joinedload(CategoryRule.category)).all()
    default_category_id = Category.query.filter_by(name='Other').first().id

    for file in files:
        if file.filename == '':
            continue

        filename = file.filename.lower()
        df = None
        imported_count_for_this_file = 0

        try:
            file.stream.seek(0)
            
            if filename.endswith('.csv'):
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                df = pd.read_csv(stream)
                
            elif filename.endswith('.pdf'):
                parsed_data = parse_chase_pdf(file.stream)
                if parsed_data:
                    df = pd.DataFrame(parsed_data)
                else:
                    flash(f'Could not parse PDF: {file.filename}. The format may be unsupported.', 'danger')
                    files_with_errors.append(file.filename)
                    continue
                    
            else:
                flash(f'Invalid file type: {file.filename}. Only .csv or .pdf files are supported.', 'warning')
                files_with_errors.append(file.filename)
                continue 

            required_columns = ['Date', 'Description', 'Amount']
            if not all(col in df.columns for col in required_columns):
                flash(f"File {file.filename} is missing required columns. Expected: {required_columns}", 'danger')
                files_with_errors.append(file.filename)
                continue
            
            for index, row in df.iterrows():
                try:
                    date_input = row['Date']
                    if isinstance(date_input, str):
                        date_str = date_input.strip()
                        try:
                            t_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                        except ValueError:
                            try:
                                t_date = datetime.strptime(date_str, '%m/%d/%Y').date()
                            except ValueError:
                                t_date = datetime.strptime(date_str, '%d-%b-%y').date()
                    elif isinstance(date_input, date):
                        t_date = date_input
                    else:
                        raise ValueError("Unknown date format")

                    t_description = str(row['Description']).strip()
                    t_amount = float(str(row['Amount']).replace('$', '').replace(',', '').strip())

                    if account.account_type == 'credit_card' and t_amount > 0:
                        t_amount = -t_amount

                    is_duplicate = Transaction.query.filter(
                        Transaction.account_id == account_id,
                        Transaction.date == t_date,
                        Transaction.description == t_description,
                        Transaction.amount == t_amount
                    ).first()
                    
                    if is_duplicate:
                        continue 

                    assigned_category_id = default_category_id
                    for rule in all_rules:
                        if rule.fragment.lower() in t_description.lower():
                            rule_cat_group = rule.category.category_group
                            
                            if (rule_cat_group == 'Income' and t_amount < 0) or \
                               (rule_cat_group == 'Expense' and t_amount > 0):
                                continue

                            assigned_category_id = rule.category_id
                            break 
                    
                    new_transaction = Transaction(
                        date=t_date,
                        description=t_description,
                        amount=t_amount,
                        category_id=assigned_category_id,
                        account_id=account_id
                    )
                    db.session.add(new_transaction)
                    imported_count_for_this_file += 1
                        
                except Exception as row_e:
                    print(f"Error processing row {index + 1} in {file.filename} ({row.get('Description')}): {row_e}")
            
            total_imported_count += imported_count_for_this_file
            total_files_processed += 1
            print(f"Successfully queued {file.filename}, imported {imported_count_for_this_file} new transactions.")

        except Exception as e:
            db.session.rollback() 
            flash(f'Error reading or processing file {file.filename}: {e}', 'danger')
            files_with_errors.append(file.filename)
            continue

    try:
        db.session.commit() 
        flash(f'Successfully processed {total_files_processed} file(s) and imported {total_imported_count} new transactions into account "{account.name}".', 'success')
        if files_with_errors:
            flash(f'Failed to process {len(files_with_errors)} file(s): {", ".join(files_with_errors)}', 'danger')
            
    except Exception as e:
        db.session.rollback()
        flash(f'A final error occurred during commit: {e}', 'danger')

    return redirect(url_for('categorize'))


@app.route('/', defaults={'month_offset': 0})
@app.route('/<month_offset>')
def index(month_offset):
    try:
        month_offset = int(month_offset)
    except ValueError:
        return redirect(url_for('index'))
    
    summary = get_monthly_summary(month_offset)
    accounts = Account.query.order_by(Account.name).all()

    plot_json_1 = create_plot('income_vs_expense', summary)
    plot_json_2 = create_plot('expense_pie', summary)
    plot_json_3 = create_plot('top_expenses', summary)
    plot_json_4 = create_plot('yoy_comparison', summary)
    
    return render_template(
        'index.html',
        month_offset=month_offset,
        current_month_name=summary['current_month_name'],
        net_worth=summary['net_worth'],
        account_balances=summary['account_balances'],
        uncategorized_count=summary['uncategorized_count'],
        plot_json_1=plot_json_1,
        plot_json_2=plot_json_2,
        plot_json_3=plot_json_3,
        plot_json_4=plot_json_4,
        accounts=accounts 
    )
    
@app.route('/edit_transactions', defaults={'month_offset': 0}, methods=['GET', 'POST'])
@app.route('/edit_transactions/<month_offset>', methods=['GET', 'POST'])
def edit_transactions(month_offset):
    month_offset = int(month_offset)
    summary = get_monthly_summary(month_offset)
    
    transactions = Transaction.query.filter(
        Transaction.date >= summary['start_date'],
        Transaction.date <= summary['end_date']
    ).order_by(Transaction.date.desc()).all()
    
    categories = Category.query.order_by(Category.category_group, Category.name).all()
    accounts = Account.query.order_by(Account.name).all()
    
    grouped_categories = {}
    for cat in categories:
        if cat.category_group not in grouped_categories:
            grouped_categories[cat.category_group] = []
        grouped_categories[cat.category_group].append(cat)
    
    return render_template(
        'edit_transactions.html', 
        transactions=transactions, 
        grouped_categories=grouped_categories, 
        accounts=accounts,
        current_month_name=summary['current_month_name'],
        month_offset=month_offset
    )

@app.route('/update_transaction/<int:t_id>', methods=['POST'])
def update_transaction(t_id):
    try:
        t = Transaction.query.get_or_404(t_id)
        month_offset = request.form.get('month_offset', 0, type=int)

        t.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        t.description = request.form['description'].strip()
        t.amount = float(str(request.form['amount']).strip())
        t.category_id = int(request.form['category_id'])
        t.account_id = int(request.form['account_id'])
        t.is_deleted = 'is_deleted' in request.form

        db.session.commit()
        flash("Transaction updated.", 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
        
    return redirect(url_for('edit_transactions', month_offset=month_offset))


@app.route('/categorize', methods=['GET', 'POST'])
def categorize():
    """Renders the page for bulk categorization of 'Other' transactions."""
    other_category = Category.query.filter(Category.name == 'Other').first()
    other_id = other_category.id if other_category else 1

    if request.method == 'POST':
        try:
            t_id = request.form['transaction_id']
            new_category_id = request.form['category_id']
            apply_rule = 'apply_rule' in request.form
            
            t = Transaction.query.get_or_404(t_id)
            
            t.category_id = new_category_id
            db.session.commit()

            if apply_rule:
                rule_text = request.form.get('rule_fragment', '').strip()
                
                if rule_text:
                    is_income_transaction = t.amount > 0

                    transactions_to_update = Transaction.query.filter(
                        Transaction.category_id == other_id, 
                        Transaction.description.ilike(f'%{rule_text}%'), 
                        Transaction.id != t.id,
                        (Transaction.amount > 0) if is_income_transaction else (Transaction.amount < 0)
                    ).all()

                    for other_t in transactions_to_update:
                        other_t.category_id = new_category_id
                    
                    db.session.commit()
                    flash(f"Rule applied! {len(transactions_to_update)} other transactions matched (checking for same sign).", 'success')
                else:
                    flash("Rule applied only to this transaction.", 'warning')
            else:
                flash(f"Transaction categorized successfully.", 'success')

        except Exception as e:
            db.session.rollback()
            flash(f"An error occurred: {e}", 'danger')
            
        return redirect(url_for('categorize'))

    transactions = Transaction.query.filter(
        Transaction.category_id == 1,
        Transaction.is_deleted == False
    ).order_by(Transaction.date.desc()).limit(100).all() 

    categories = Category.query.order_by(Category.category_group, Category.name).all()

    grouped_categories = {}
    for cat in categories:
        if cat.category_group not in grouped_categories:
            grouped_categories[cat.category_group] = []
        grouped_categories[cat.category_group].append(cat)
    
    existing_groups = db.session.query(Category.category_group).distinct().order_by(Category.category_group).all()
    existing_groups = [g[0] for g in existing_groups]

    return render_template('categorize.html', 
                           transactions=transactions, 
                           grouped_categories=grouped_categories, 
                           existing_groups=existing_groups,
                           month_offset=0)

# --- Event Management Routes ---

@app.route('/events')
def events():
    """Renders the page to manage Dates of Interest."""
    all_events = Event.query.order_by(Event.date.desc()).all()
    return render_template('events.html', events=all_events)

@app.route('/add_event', methods=['POST'])
def add_event():
    """Adds a new date of interest."""
    try:
        date_str = request.form.get('date')
        description = request.form.get('description', '').strip()

        if not date_str or not description:
            flash('Date and Description are required.', 'danger')
            return redirect(url_for('events'))

        event_date = datetime.strptime(date_str, '%Y-%m-%d').date()

        new_event = Event(date=event_date, description=description)
        db.session.add(new_event)
        db.session.commit()

        flash(f'Event "{description}" added successfully.', 'success')

    except ValueError:
        flash('Invalid date format.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')

    return redirect(url_for('events'))

@app.route('/delete_event/<int:event_id>', methods=['POST'])
def delete_event(event_id):
    """Deletes an event."""
    try:
        event = Event.query.get_or_404(event_id)
        db.session.delete(event)
        db.session.commit()
        flash('Event deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting event: {e}', 'danger')
        
    return redirect(url_for('events'))

# --- Trend Analysis Routes ---

@app.route('/trends')
def trends():
    """Renders the new Trend Analysis page."""
    categories = Category.query.order_by(Category.category_group, Category.name).all()
    grouped_categories = {}
    for cat in categories:
        if cat.category_group not in grouped_categories:
            grouped_categories[cat.category_group] = []
        grouped_categories[cat.category_group].append(cat)
        
    all_events = Event.query.order_by(Event.date.desc()).all()
        
    return render_template('trends.html', 
                           grouped_categories=grouped_categories,
                           all_events=all_events)

@app.route('/api/trend_data', methods=['POST'])
def get_trend_data():
    """API endpoint to fetch and aggregate data for the trends chart."""
    try:
        data = request.get_json()
        category_ids = data.get('category_ids', [])
        event_ids = [int(e_id) for e_id in data.get('event_ids', [])]
        start_date_str = data.get('start_date')
        end_date_str = data.get('end_date')
        time_bucket = data.get('time_bucket', 'month')

        if not all([category_ids, start_date_str, end_date_str]):
            return jsonify({'error': 'Missing required parameters.'}), 400

        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

        query = db.session.query(
            func.date_trunc(time_bucket, Transaction.date).label('bucket_start'),
            Category.name,
            func.sum(Transaction.amount).label('total')
        ).join(Category).filter(
            Transaction.date >= start_date,
            Transaction.date <= end_date,
            Transaction.is_deleted == False,
            Transaction.category_id.in_(category_ids)
        ).group_by(
            'bucket_start',
            Category.name
        ).order_by('bucket_start')

        results = query.all()

        data_by_cat = {}
        all_buckets = set()
        
        for row in results:
            bucket_date = row.bucket_start.strftime('%Y-%m-%d')
            cat_name = row.name
            total = float(row.total)
            if total < 0:
                total = abs(total)
            
            all_buckets.add(bucket_date)
            
            if cat_name not in data_by_cat:
                data_by_cat[cat_name] = {}
            data_by_cat[cat_name][bucket_date] = total
            
        sorted_buckets = sorted(list(all_buckets))

        plot_data = []
        for cat_name, bucket_values in data_by_cat.items():
            x_values = sorted_buckets
            y_values = [bucket_values.get(bucket, 0) for bucket in sorted_buckets]
            
            plot_data.append({
                'type': 'bar',
                'name': cat_name,
                'x': x_values,
                'y': y_values
            })
            
        events = Event.query.filter(
            Event.date >= start_date,
            Event.date <= end_date,
            Event.id.in_(event_ids)
        ).all()

        layout_shapes = []
        layout_annotations = []
        for event in events.copy():
            event_date_str = event.date.strftime('%Y-%m-%d')
            layout_shapes.append({
                'type': 'line',
                'x0': event_date_str,
                'x1': event_date_str,
                'y0': 0,
                'y1': 1,
                'yref': 'paper',
                'line': {
                    'color': 'rgb(236, 72, 153)',
                    'width': 2,
                    'dash': 'dot'
                }
            })
            layout_annotations.append({
                'x': event_date_str,
                'y': 0.95,
                'yref': 'paper',
                'text': event.description,
                'showarrow': False,
                'xanchor': 'left',
                'bgcolor': 'rgba(236, 72, 153, 0.8)',
                'font': {'color': 'white'}
            })

        return jsonify({
            'plot_data': plot_data,
            'layout_shapes': layout_shapes,
            'layout_annotations': layout_annotations
        })

    except Exception as e:
        print(f"Error in /api/trend_data: {e}")
        return jsonify({'error': str(e)}), 500

# --- API Routes for Insights ---

@app.route('/api/insight_data')
def api_insight_data():
    """Provides raw spending data for the AI insight generator."""
    try:
        data = get_insight_data()
        return jsonify(data)
    except Exception as e:
        print(f"Error in /api/insight_data: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/yearly_insight_data')
def api_yearly_insight_data():
    """Provides raw YTD vs. last YTD spending data for AI insights."""
    try:
        data = get_yearly_insight_data()
        return jsonify(data)
    except Exception as e:
        print(f"Error in /api/yearly_insight_data: {e}")
        return jsonify({'error': str(e)}), 500


# --- Main Run ---
if __name__ == '__main__':
    with app.app_context():
        create_tables() 
    app.run(debug=True)