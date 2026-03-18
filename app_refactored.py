import os
import calendar
import io
import json
from datetime import datetime, date, timedelta
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from sqlalchemy import func, extract, case, or_, text
from dotenv import load_dotenv

# Import models and database
from models import (
    db, Account, StatementRecord, Category, Entity, Transaction, Event,
    Budget, BudgetPlan, BudgetLineItem, create_tables
)

# Import utilities
from utils.helpers import (
    get_uncategorized_id, try_parse_date, find_or_create_entity,
    update_entity_patterns, apply_entity_to_transactions, rematch_all_entities,
    get_monthly_summary_direct, get_spending_data_for_period,
    EXCLUDED_CAT
)
from utils.pdf_parsers import parse_chase_pdf, parse_wellsfargo_pdf, parse_hsa_pdf

# Import services
from services.dashboard import DashboardService

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://budget_user:ben@localhost:5432/budget_db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24)

# Initialize database with app
db.init_app(app)

# Create tables on first run
with app.app_context():
    create_tables()

# Helper function for bucket generation
def generate_buckets(start_date, end_date, bucket_type):
    current = start_date
    buckets = []
    
    if bucket_type == 'year':
        current = current.replace(month=1, day=1)
    elif bucket_type == 'month':
        current = current.replace(day=1)
    elif bucket_type == 'week':
        idx = (current.weekday() + 1) % 7
        current = current - timedelta(days=idx)

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

# ─── ROUTES ─────────────────────────────────────────────────────────────

@app.route('/', defaults={'month_offset': '0'})
@app.route('/<string:month_offset>')
def index(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    
    view_mode = request.args.get('view', 'monthly')
    
    service = DashboardService(view_mode=view_mode)
    summary = service.get_summary_for_dashboard(month_offset)
    charts = service.generate_all_charts()
    
    return render_template('index.html', month_offset=month_offset, summary=summary, accounts=Account.query.all(), gemini_api_key=os.getenv('GEMINI_API_KEY'), view_mode=view_mode, **charts)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    account_id = request.form.get('account_id')
    files = request.files.getlist('file')
    
    manual_start_str = request.form.get('start_date')
    manual_end_str = request.form.get('end_date')
    manual_start = try_parse_date(manual_start_str)
    manual_end = try_parse_date(manual_end_str)

    csv_has_header = request.form.get('csv_has_header') == 'on'

    if not account_id or not files: return redirect(url_for('index'))
    account = db.session.get(Account, account_id)
    if not account: abort(404)
    uncat_id = get_uncategorized_id()
    added = 0
    
    for file in files:
        try:
            df = pd.DataFrame()
            p_start, p_end = None, None

            if file.filename.lower().endswith('.csv'):
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                df = pd.read_csv(stream, header=0 if csv_has_header else None)
                
                if df.shape[1] >= 5:
                    df = df.iloc[:, [0, 1, 4]]
                    df.columns = ['Date', 'Amount', 'Description']
                
                if manual_start and manual_end:
                    p_start, p_end = manual_start, manual_end
                
            elif file.filename.lower().endswith('.pdf'):
                data = None
                if account.account_type == 'hsa':
                    data, parsed_start, parsed_end = parse_hsa_pdf(file.stream)
                elif account.account_type in ['checking', 'savings']:
                    data, parsed_start, parsed_end = parse_wellsfargo_pdf(file.stream)
                else:
                    data, parsed_start, parsed_end = parse_chase_pdf(file.stream)
                
                p_start = parsed_start if parsed_start else manual_start
                p_end = parsed_end if parsed_end else manual_end

                if data: df = pd.DataFrame(data)
            
            period_exists = False
            if p_start and p_end:
                existing = StatementRecord.query.filter_by(account_id=account.id, start_date=p_start, end_date=p_end).first()
                if existing:
                    period_exists = True
                    flash(f"Note: Statement period {p_start} - {p_end} already uploaded. Checking for new transactions...", "info")
                else:
                    db.session.add(StatementRecord(account_id=account.id, start_date=p_start, end_date=p_end))
                    db.session.commit()
            elif file.filename.lower().endswith('.csv') and (not manual_start or not manual_end):
                 flash(f"Warning: CSV uploaded without date range. Statement history not updated for {file.filename}.", "warning")

            if df.empty: continue

            new_transactions = []
            skipped_duplicates = 0
            errors_count = 0
            for idx, row in df.iterrows():
                try:
                    dvals = row.get('Date')
                    date_val = pd.to_datetime(dvals).date() if isinstance(dvals, str) else dvals
                    desc = str(row.get('Description', '')).strip()
                    amount = float(str(row.get('Amount')).replace('$','').replace(',',''))
                    
                    if account.account_type == 'credit_card' and file.filename.lower().endswith('.csv') and amount > 0:
                        amount = -amount 
                    
                    duplicate = Transaction.query.filter_by(
                        account_id=account.id,
                        date=date_val,
                        amount=amount,
                        original_description=desc
                    ).first()
                    
                    if duplicate:
                        skipped_duplicates += 1
                        continue
                    
                    entity, cat_id = find_or_create_entity(desc, amount, uncat_id)
                    
                    new_transactions.append(Transaction(date=date_val, original_description=desc, amount=amount, entity_id=entity.id, category_id=cat_id, account_id=account.id))
                    added += 1
                except Exception as e:
                    errors_count += 1
                    flash(f"Row {idx}: Error processing transaction - {str(e)[:100]}", "warning")
                    continue
            
            if new_transactions:
                db.session.add_all(new_transactions)
                db.session.commit()
            
            if skipped_duplicates > 0:
                flash(f"{file.filename}: Skipped {skipped_duplicates} duplicate transaction(s), imported {len(new_transactions)} new transaction(s).", "info")
                
        except Exception as e:
            db.session.rollback()
            flash(f"File Error {file.filename}: {e}", "danger")
            
    if added > 0: flash(f"Imported {added} transactions.", "success")
    return redirect(url_for('index'))

@app.route('/manage_entities', methods=['GET', 'POST'])
def manage_entities():
    if request.method == 'POST':
        entity_id = request.form.get('entity_id')
        name = request.form.get('name', '').strip()
        category_id = request.form.get('category_id')
        match_type = request.form.get('match_type', 'any')
        patterns_str = request.form.get('match_patterns', '').strip()
        
        patterns = [p.strip().upper() for p in patterns_str.split(',') if p.strip()] if patterns_str else []
        
        if entity_id:
            entity = db.session.get(Entity, int(entity_id))
            if entity and name:
                entity.name = name
                entity.category_id = int(category_id) if category_id else entity.category_id
                entity.match_type = match_type
                entity.match_patterns = patterns
                entity.is_auto_created = False
                db.session.commit()
                
                count = apply_entity_to_transactions(entity.id, entity.category_id)
                flash(f"Updated entity '{name}'. Applied to {count} transactions.", "success")
        elif name and category_id:
            existing = Entity.query.filter_by(name=name).first()
            if not existing:
                entity = Entity(
                    name=name,
                    category_id=int(category_id),
                    match_type=match_type,
                    match_patterns=patterns,
                    is_auto_created=False
                )
                db.session.add(entity)
                db.session.commit()
                flash(f"Created entity '{name}'.", "success")
            else:
                flash(f"Entity '{name}' already exists.", "danger")
        
        return redirect(url_for('manage_entities'))
    
    search_query = request.args.get('search', '').strip()
    query = Entity.query.join(Category)
    
    if search_query:
        term = f"%{search_query}%"
        query = query.filter(or_(
            Entity.name.ilike(term),
            Category.name.ilike(term)
        ))
    
    entities = query.order_by(Entity.is_auto_created.desc(), Entity.name).all()
    
    page = request.args.get('page', 1, type=int)
    per_page = 100
    total = len(entities)
    start = (page - 1) * per_page
    end = start + per_page
    entities_paginated = entities[start:end]
    has_next = end < total
    has_prev = start > 0
    
    cats = Category.query.order_by(Category.type, Category.name).all()
    grouped_categories = {}
    for c in cats:
        if c.type not in grouped_categories: grouped_categories[c.type] = []
        grouped_categories[c.type].append(c)
    
    return render_template('manage_entities.html', 
                         entities=entities_paginated, 
                         grouped_categories=grouped_categories,
                         search_query=search_query, 
                         page=page, 
                         has_next=has_next, 
                         has_prev=has_prev)

@app.route('/manage_entities/delete/<int:entity_id>', methods=['POST'])
def delete_entity(entity_id):
    uncat = get_uncategorized_id()
    entity = db.session.get(Entity, entity_id)
    if entity:
        Transaction.query.filter_by(entity_id=entity_id).update({'category_id': uncat})
        db.session.delete(entity)
        db.session.commit()
        flash(f"Deleted entity '{entity.name}'.", "success")
    return redirect(url_for('manage_entities'))

@app.route('/manage_entities/apply/<int:entity_id>', methods=['POST'])
def apply_entity_patterns(entity_id):
    entity = db.session.get(Entity, entity_id)
    if entity:
        count = apply_entity_to_transactions(entity_id, entity.category_id)
        flash(f"Applied patterns for '{entity.name}' to {count} transactions.", "success")
    return redirect(url_for('manage_entities'))

@app.route('/api/rematch_all_entities', methods=['POST'])
def api_rematch_all_entities():
    try:
        total = rematch_all_entities()
        flash(f'Re-matched {total} transactions.', 'success')
        return jsonify({'success': True, 'message': f'Re-matched {total} transactions.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

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

# Continue with remaining routes from original app.py...
# (Statement history, categorize, edit_transactions, monthly_averages, budget routes, etc.)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
