import os
import calendar
import io
import json
import pandas as pd
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from sqlalchemy import func, extract, case, or_, text, exists
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified
from dotenv import load_dotenv

from constants import DASHBOARD_MONTH_SPAN, EXCLUDED_CAT, EXCLUDED_CAT_CORE, EXCLUDED_PAYEE_LABELS_CORE
from models import db, Account, StatementRecord, Category, Entity, Transaction, Event, Budget, BudgetPlan, BudgetLineItem, create_tables
from utils.helpers import get_uncategorized_id, try_parse_date, find_or_create_entity, update_entity_patterns, apply_entity_to_transactions, auto_match_transactions_to_entity, rematch_all_entities, get_monthly_summary_direct
from utils.pdf_parsers import parse_chase_pdf, parse_wellsfargo_pdf, parse_hsa_pdf, parse_fidelity_csv
from services.dashboard import DashboardService

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://budget_user:ben@localhost:5432/budget_db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24)

db.init_app(app)


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
        # Skip excluded category
        if t.category.name == EXCLUDED_CAT:
            continue
            
        if t.amount < 0:
            spending_by_cat[t.category.name] = spending_by_cat.get(t.category.name, 0) + abs(float(t.amount))
        elif t.amount > 0:
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
        # Align to Sunday
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

# Update the Index Route to handle the 'view' parameter
@app.route('/', defaults={'month_offset': '0'})
@app.route('/<string:month_offset>')
def index(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    
    view_mode = request.args.get('view', 'monthly')
    year = request.args.get('year', type=int)
    
    service = DashboardService(view_mode=view_mode, year=year)
    summary = service.get_summary_for_dashboard(month_offset)
    charts = service.generate_all_charts()
    
    return render_template('index.html', month_offset=month_offset, summary=summary, accounts=Account.query.all(), gemini_api_key=os.getenv('GEMINI_API_KEY'), view_mode=view_mode, current_year=service.display_year, **charts)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    account_id = request.form.get('account_id')
    files = request.files.getlist('file')
    
    # Capture manual dates if provided (specifically for CSVs)
    manual_start_str = request.form.get('start_date')
    manual_end_str = request.form.get('end_date')
    manual_start = try_parse_date(manual_start_str)
    manual_end = try_parse_date(manual_end_str)

    # Check if CSV has header row
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

            # 1. Handle CSV (Uses Manual Dates)
            if file.filename.lower().endswith('.csv'):
                if account.account_type == 'brokerage':
                    data, parsed_start, parsed_end = parse_fidelity_csv(file.stream)
                    p_start = parsed_start if parsed_start else manual_start
                    p_end = parsed_end if parsed_end else manual_end
                    if data:
                        df = pd.DataFrame(data)
                else:
                    stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                    # Use header parameter based on checkbox
                    df = pd.read_csv(stream, header=0 if csv_has_header else None)
                    
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
            period_exists = False
            if p_start and p_end:
                # Check for exact duplicate period
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

            # 4. Transaction Processing with Individual Duplicate Detection
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
                    
                    # Check if this specific transaction already exists
                    # Match on: account_id, date, amount, and original_description
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
        
        # Parse patterns (comma-separated)
        patterns = [p.strip().upper() for p in patterns_str.split(',') if p.strip()] if patterns_str else []
        
        if entity_id:
            # Update existing entity
            entity = db.session.get(Entity, int(entity_id))
            if entity and name:
                # Check if renaming to a name that already exists on a different entity
                existing_entity = Entity.query.filter_by(name=name).first()
                if existing_entity and existing_entity.id != entity.id:
                    flash(f"Cannot rename to '{name}' - an entity with that name already exists.", "danger")
                    return redirect(url_for('manage_entities'))
                
                entity.name = name
                entity.category_id = int(category_id) if category_id else entity.category_id
                entity.match_type = match_type
                entity.match_patterns = patterns
                entity.is_auto_created = False
                db.session.commit()
                
                # Apply to all transactions with this entity
                count = apply_entity_to_transactions(entity.id, entity.category_id)
                
                # Auto-match unassigned transactions based on patterns
                matched_count = auto_match_transactions_to_entity(entity)
                
                if matched_count > 0:
                    flash(f"Updated entity '{name}'. Applied to {count} existing transactions and auto-matched {matched_count} unassigned transactions.", "success")
                else:
                    flash(f"Updated entity '{name}'. Applied to {count} transactions.", "success")
        elif name and category_id:
            # Create new entity
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
                
                # Auto-match unassigned transactions based on patterns
                matched_count = auto_match_transactions_to_entity(entity)
                
                if matched_count > 0:
                    flash(f"Created entity '{name}' and auto-matched {matched_count} unassigned transactions.", "success")
                else:
                    flash(f"Created entity '{name}'.", "success")
            else:
                flash(f"Entity '{name}' already exists.", "danger")
        
        return redirect(url_for('manage_entities'))
    
    # GET request - display entities
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
    
    cats = Category.query.order_by(Category.name).all()
    
    return render_template('manage_entities.html', 
                         entities=entities_paginated, 
                         categories=cats,
                         search_query=search_query, 
                         page=page, 
                         has_next=has_next, 
                         has_prev=has_prev)

@app.route('/manage_entities/delete/<int:entity_id>', methods=['POST'])
def delete_entity(entity_id):
    uncat = get_uncategorized_id()
    entity = db.session.get(Entity, entity_id)
    if entity:
        # Use bulk update to reassign transactions to uncategorized + auto-entities
        transactions = Transaction.query.filter_by(entity_id=entity_id).all()
        
        # Create auto-entities for unique descriptions
        for t in transactions:
            desc = t.original_description if t.original_description else f"Transaction {t.id}"
            auto_entity = Entity.query.filter_by(name=desc).first()
            if not auto_entity:
                auto_entity = Entity(
                    name=desc,
                    category_id=uncat,
                    is_auto_created=True,
                    match_patterns=[]
                )
                db.session.add(auto_entity)
        
        db.session.commit()  # Commit all new entities
        
        # Update transactions using bulk update for each unique description
        for t in transactions:
            desc = t.original_description if t.original_description else f"Transaction {t.id}"
            auto_entity = Entity.query.filter_by(name=desc).first()
            if not auto_entity:
                raise Exception(f"Failed to find/create auto-entity '{desc}' for transaction {t.id}")
            Transaction.query.filter_by(id=t.id).update(
                {'entity_id': auto_entity.id, 'category_id': uncat},
                synchronize_session=False
            )
        
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

@app.route('/api/sync_entity_categories', methods=['POST'])
def sync_entity_categories():
    """Sync entity categories to match their transactions' most common category"""
    try:
        cat_map = {c.id: c.name for c in Category.query.all()}

        dominant = db.session.query(
            Transaction.entity_id,
            Transaction.category_id,
            func.count(Transaction.id).label('cnt')
        ).filter(Transaction.is_deleted == False)\
         .group_by(Transaction.entity_id, Transaction.category_id)\
         .order_by(Transaction.entity_id, func.count(Transaction.id).desc())\
         .all()

        best_cat = {}
        for row in dominant:
            if row.entity_id not in best_cat:
                best_cat[row.entity_id] = row.category_id

        entities = Entity.query.all()
        synced = 0
        skipped = 0
        details = []

        for entity in entities:
            top_cat_id = best_cat.get(entity.id)
            if top_cat_id is None:
                skipped += 1
            elif top_cat_id != entity.category_id:
                old_name = cat_map.get(entity.category_id, 'None')
                new_name = cat_map.get(top_cat_id, 'None')
                entity.category_id = top_cat_id
                synced += 1
                details.append(f"{entity.name}: {old_name} → {new_name}")
        
        db.session.commit()
        msg = f'Synced {synced} entities, skipped {skipped} with no transactions.'
        if details and len(details) <= 10:
            msg += f'\n\nUpdated:\n' + '\n'.join(details[:10])
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/rematch_all_entities', methods=['POST'])
def api_rematch_all_entities():
    try:
        total = rematch_all_entities()
        flash(f'Re-matched {total} transactions.', 'success')
        return jsonify({'success': True, 'message': f'Re-matched {total} transactions.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/delete_orphaned_entities', methods=['POST'])
def delete_orphaned_entities():
    """Delete entities that have no transactions"""
    try:
        orphaned = Entity.query.filter(
            ~exists().where(
                (Transaction.entity_id == Entity.id) &
                (Transaction.is_deleted == False)
            )
        ).all()
        deleted = [e.name for e in orphaned]
        for entity in orphaned:
            db.session.delete(entity)
        
        db.session.commit()
        count = len(deleted)
        msg = f'Deleted {count} orphaned entities with no transactions.'
        if deleted and len(deleted) <= 10:
            msg += f'\n\nDeleted:\n' + '\n'.join(deleted[:10])
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/statement_history')
def statement_history():
    page = request.args.get('page', 1, type=int)
    account_filter = request.args.get('account_id', type=int)
    year_filter = request.args.get('year', type=int)
    per_page = 20

    query = db.session.query(StatementRecord).join(Account).order_by(StatementRecord.start_date.desc())

    if account_filter:
        query = query.filter(StatementRecord.account_id == account_filter)
    
    if year_filter:
        query = query.filter(extract('year', StatementRecord.start_date) == year_filter)

    total_records = query.count()
    start = (page - 1) * per_page
    end = start + per_page
    records = query.slice(start, end).all()
    
    accounts = Account.query.order_by(Account.name).all()
    available_years_query = db.session.query(extract('year', StatementRecord.start_date)).distinct().order_by(extract('year', StatementRecord.start_date).desc()).all()
    available_years = [int(y[0]) for y in available_years_query]

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
                        # Find or create entity with this display name
                        entity = Entity.query.filter_by(name=disp).first()
                        
                        # Normalize pattern to uppercase for consistent matching
                        frag_upper = frag.strip().upper()
                        
                        if entity:
                            # Update existing entity
                            existing_patterns = entity.match_patterns or []
                            if frag_upper not in existing_patterns:
                                existing_patterns.append(frag_upper)
                                entity.match_patterns = existing_patterns
                                # Mark the JSON field as modified so SQLAlchemy detects the change
                                flag_modified(entity, 'match_patterns')
                            entity.category_id = cat_id
                            entity.match_type = 'any'
                            entity.is_auto_created = False
                            flash_msg = f"Updated entity '{disp}'."
                        else:
                            # Create new entity
                            entity = Entity(
                                name=disp,
                                category_id=cat_id,
                                match_type='any',
                                match_patterns=[frag_upper],
                                is_auto_created=False
                            )
                            db.session.add(entity)
                            flash_msg = f"Created entity '{disp}'."
                        
                        db.session.commit()
                        
                        # Update this transaction
                        t.entity_id = entity.id
                        t.category_id = cat_id
                        
                        # Apply to all matching transactions
                        count = apply_entity_to_transactions(entity.id, cat_id)
                        
                        # Auto-match unassigned transactions based on patterns
                        matched_count = auto_match_transactions_to_entity(entity)
                        
                        db.session.commit()
                        
                        if matched_count > 0:
                            flash(f"{flash_msg} Applied to {count} transactions and auto-matched {matched_count} unassigned transactions.", "success")
                        else:
                            flash(f"{flash_msg} Applied to {count} transactions.", "success")
            except ValueError: flash("Invalid Category ID.", "danger")
            except Exception as e: db.session.rollback(); flash(f"Error: {e}", "danger")
        else: flash("Missing required fields.", "danger")
        return redirect(url_for('categorize', filter_type=c_filt, search=c_srch, year=c_year, month=c_month))

    filt = request.args.get('filter_type', 'all')
    srch = request.args.get('search', '').strip()
    year = request.args.get('year', '')
    month = request.args.get('month', '')
    uncat_category = Category.query.filter_by(name='Uncategorized').first()
    q = Transaction.query.join(Entity).filter(Transaction.is_deleted == False)
    
    if not srch and uncat_category: 
        q = q.filter(Transaction.category_id == uncat_category.id)
        
    if filt == 'positive': q = q.filter(Transaction.amount > 0)
    elif filt == 'negative': q = q.filter(Transaction.amount < 0)
    if year and year != 'all': q = q.filter(extract('year', Transaction.date) == int(year))
    if month and month != 'all': q = q.filter(extract('month', Transaction.date) == int(month))
    if srch: q = q.filter(or_(Entity.name.ilike(f"%{srch}%"), Transaction.original_description.ilike(f"%{srch}%")))
    txs = q.order_by(Transaction.date.desc()).limit(500 if (srch or year or month) else 50).all()
    cats = Category.query.order_by(Category.name).all()
    all_entities = db.session.query(Entity.name, Entity.category_id).order_by(Entity.name).all()
    labels = [e.name for e in all_entities]
    entity_category_map = {e.name: e.category_id for e in all_entities}
    available_years = [int(y[0]) for y in db.session.query(extract('year', Transaction.date)).distinct().order_by(extract('year', Transaction.date).desc()).all()]
    month_choices = [(str(i), calendar.month_name[i]) for i in range(1, 13)]
    
    return render_template('categorize.html', transactions=txs, categories=cats, filter_type=filt, search_query=srch, selected_year=year, selected_month=month, available_years=available_years, existing_labels=labels, month_choices=month_choices, entity_category_map=entity_category_map)

@app.route('/edit_transactions', defaults={'month_offset': '0'}, methods=['GET','POST'])
@app.route('/edit_transactions/<string:month_offset>', methods=['GET','POST'])
def edit_transactions(month_offset):
    try: month_offset = int(month_offset)
    except: return redirect(url_for('index'))
    summary = get_monthly_summary_direct(month_offset)
    srch = request.args.get('search', '').strip()
    if srch:
        current_context = f"Search Results: '{srch}'"
        query = Transaction.query.join(Entity).filter(
            or_(
                Entity.name.ilike(f"%{srch}%"),
                Transaction.original_description.ilike(f"%{srch}%")
            )
        )
    else:
        req_year = request.args.get('year')
        req_month = request.args.get('month')
        if req_year and req_month:
             query = Transaction.query.join(Entity).filter(extract('year', Transaction.date) == int(req_year), extract('month', Transaction.date) == int(req_month))
             current_context = f"Filter: {calendar.month_name[int(req_month)]} {req_year}"
        elif req_year:
             query = Transaction.query.join(Entity).filter(extract('year', Transaction.date) == int(req_year))
             current_context = f"Filter: {req_year}"
        else:
             current_context = summary['current_month_name']
             query = Transaction.query.join(Entity).filter(Transaction.date >= summary['start_date'], Transaction.date <= summary['end_date'])

    sort_by = request.args.get('sort_by', 'date')
    sort_order = request.args.get('sort_order', 'desc')

    if sort_by == 'payee': sort_attr = Entity.name
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
    cats = Category.query.order_by(Category.name).all()
        
    return render_template('edit_transactions.html', transactions=txs, entities=Entity.query.order_by(Entity.name).all(), categories=cats, accounts=Account.query.all(), month_offset=month_offset, current_month_name=current_context, search_query=srch, available_years=available_years, month_choices=month_choices, selected_year=request.args.get('year'), selected_month=request.args.get('month'), sort_by=sort_by, sort_order=sort_order)

@app.route('/monthly_averages')
def monthly_averages():
    cats = Category.query.order_by(Category.name).all()
    
    accounts = Account.query.order_by(Account.name).all()
    
    # Get Entities mapped to Categories
    # Structure: { category_id: [list of entity names] }
    payee_map = {}
    
    # Query Entities joined with Categories
    entities = db.session.query(Entity.category_id, Entity.name).order_by(Entity.name).all()
    for e in entities:
        if e.category_id not in payee_map: payee_map[e.category_id] = []
        if e.name not in payee_map[e.category_id]:
            payee_map[e.category_id].append(e.name)
            
    # Also fetch distinct entity names for the generic list
    results = db.session.query(Entity.name).distinct().order_by(Entity.name).all()
    unique_labels = [r.name for r in results if r.name]
    
    budgets = Budget.query.order_by(Budget.name).all()
    
    return render_template('monthly_averages.html', categories=cats, payee_labels=unique_labels, accounts=accounts, budgets=budgets, category_payees=payee_map)

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
            q = q.join(Entity)
            q = q.filter(Entity.name.notin_(payee_names))
            
        if excluded_payees:
            if not payee_names: q = q.join(Entity)
            q = q.filter(Entity.name.notin_(excluded_payees))

        q = q.filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False)\
            .filter(Transaction.category_id.in_(cat_ids))
        
        if acct_ids:
            q = q.filter(Transaction.account_id.in_(acct_ids))
            
        q = q.group_by(Category.id, Category.name)
        
        for row in q.all():
            total = abs(float(row.total)) if row.total else 0.0
            
            # Sub-query for Entity Breakdown: DO NOT filter excluded_payees here!
            # We want them in the list so they can be rendered (unchecked)
            payee_q = db.session.query(
                Entity.name.label('payee_name'),
                func.sum(Transaction.amount).label('ptotal')
            ).select_from(Transaction).join(Entity)\
             .filter(Transaction.category_id == row.id) \
             .filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False)

            if acct_ids:
                payee_q = payee_q.filter(Transaction.account_id.in_(acct_ids))

            if payee_names:
                 payee_q = payee_q.filter(Entity.name.notin_(payee_names))
            
            # IMPORTANT: We DO NOT apply excluded_payees filter here.
            # This allows the frontend to receive the data for excluded items so it can show them as unchecked rows.

            payee_q = payee_q.group_by(Entity.name)
            
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
        q = db.session.query(Entity.name.label('name'), func.sum(Transaction.amount).label('total'))\
            .select_from(Transaction).join(Entity)\
            .filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False)\
            .filter(Entity.name.in_(payee_names))
            
        if acct_ids:
            q = q.filter(Transaction.account_id.in_(acct_ids))
            
        q = q.group_by(Entity.name)
        
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
        name = request.form.get('entity_name_input', '').strip().title()
        if name:
            # Find or create entity
            entity = Entity.query.filter_by(name=name).first()
            if not entity:
                entity = Entity(name=name, category_id=get_uncategorized_id(), is_auto_created=True, match_patterns=[])
                db.session.add(entity)
                db.session.commit()
            t.entity_id = entity.id
        
        t.category_id = int(request.form.get('category_id'))
        t.account_id = int(request.form.get('account_id'))
        t.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        t.amount = float(request.form.get('amount'))
        t.is_deleted = 'is_deleted' in request.form
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

# ── Budget Plan Management ──────────────────────────────────────────────

@app.route('/budget')
def budget_page():
    plans = BudgetPlan.query.order_by(BudgetPlan.name).all()
    active_plan = BudgetPlan.query.filter_by(is_active=True).first()
    cats = Category.query.order_by(Category.name).all()
    entities = Entity.query.order_by(Entity.name).all()
    return render_template('budget.html', plans=plans, active_plan=active_plan,
                           categories=cats,
                           entities=entities)

@app.route('/api/budget_plan', methods=['POST'])
def api_create_budget_plan():
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'message': 'Name is required.'})
    if BudgetPlan.query.filter_by(name=name).first():
        return jsonify({'success': False, 'message': 'A plan with that name already exists.'})
    plan = BudgetPlan(name=name)
    db.session.add(plan)
    db.session.commit()
    return jsonify({'success': True, 'id': plan.id})

@app.route('/trends')
def trends():
    cats = Category.query.order_by(Category.name).all()
    entities = Entity.query.order_by(Entity.name).all()
    return render_template('trends.html', 
                         categories=cats, 
                         entities=entities,
                         excluded_core=EXCLUDED_CAT_CORE,
                         excluded_cat=EXCLUDED_CAT)

@app.route('/api/budget_plan/<int:plan_id>/activate', methods=['POST'])
def api_activate_budget_plan(plan_id):
    BudgetPlan.query.update({BudgetPlan.is_active: False})
    plan = db.session.get(BudgetPlan, plan_id)
    if not plan:
        return jsonify({'success': False, 'message': 'Plan not found.'})
    plan.is_active = True
    db.session.commit()
    return jsonify({'success': True, 'message': f'"{plan.name}" is now the active budget.'})

@app.route('/api/budget_plan/<int:plan_id>/delete', methods=['POST'])
def api_delete_budget_plan(plan_id):
    plan = db.session.get(BudgetPlan, plan_id)
    if plan:
        db.session.delete(plan)
        db.session.commit()
    return jsonify({'success': True})

@app.route('/api/budget_plan/<int:plan_id>/items')
def api_get_budget_plan_items(plan_id):
    items = BudgetLineItem.query.filter_by(budget_id=plan_id).order_by(BudgetLineItem.item_type, BudgetLineItem.label).all()
    result = []
    for i in items:
        result.append({
            'id': i.id,
            'label': i.label,
            'item_type': i.item_type,
            'category_id': i.category_id,
            'category_name': i.category.name if i.category else None,
            'entity_id': i.entity_id,
            'entity_name': i.entity.name if i.entity else None,
            'expected_amount': float(i.expected_amount),
            'frequency': getattr(i, 'frequency', 'monthly'),
            'notes': i.notes
        })
    return jsonify({'items': result})

@app.route('/api/budget_item', methods=['POST'])
def api_create_budget_item():
    data = request.get_json()
    item = BudgetLineItem(
        budget_id=data['budget_id'],
        label=data.get('label', '').strip(),
        item_type=data.get('item_type', 'expense'),
        category_id=data.get('category_id') or None,
        entity_id=data.get('entity_id') or None,
        expected_amount=float(data.get('expected_amount', 0)),
        frequency=data.get('frequency', 'monthly'),
        notes=data.get('notes', '').strip()
    )
    db.session.add(item)
    db.session.commit()
    return jsonify({'success': True, 'id': item.id})

@app.route('/api/budget_item/<int:item_id>', methods=['PUT'])
def api_update_budget_item(item_id):
    item = db.session.get(BudgetLineItem, item_id)
    if not item:
        return jsonify({'success': False, 'message': 'Item not found.'})
    data = request.get_json()
    item.label = data.get('label', item.label).strip()
    item.item_type = data.get('item_type', item.item_type)
    item.category_id = data.get('category_id') or None
    item.entity_id = data.get('entity_id') or None
    item.expected_amount = float(data.get('expected_amount', item.expected_amount))
    item.frequency = data.get('frequency', getattr(item, 'frequency', 'monthly'))
    item.notes = data.get('notes', '').strip()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/budget_item/<int:item_id>', methods=['DELETE'])
def api_delete_budget_item(item_id):
    item = db.session.get(BudgetLineItem, item_id)
    if item:
        db.session.delete(item)
        db.session.commit()
    return jsonify({'success': True})

@app.route('/api/budget_plan/<int:plan_id>/import_csv', methods=['POST'])
def api_import_csv(plan_id):
    data = request.get_json()
    csv_text = data.get('csv_text', '')
    lines = [l.strip() for l in csv_text.strip().split('\n') if l.strip()]
    count = 0
    for line in lines:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 3:
            continue
        label = parts[0]
        item_type = parts[1].lower()
        if item_type not in ('income', 'expense'):
            continue
        try:
            amount = float(parts[2].replace('$', '').replace(',', ''))
        except ValueError:
            continue
        notes = parts[3] if len(parts) > 3 else ''
        item = BudgetLineItem(budget_id=plan_id, label=label, item_type=item_type,
                              expected_amount=amount, notes=notes)
        db.session.add(item)
        count += 1
    db.session.commit()
    return jsonify({'success': True, 'message': f'Imported {count} line items.'})

@app.route('/api/budget_plan/<int:plan_id>/populate_from_averages', methods=['POST'])
def api_populate_from_averages(plan_id):
    data = request.get_json()
    months = int(data.get('months', 6))
    cutoff = date.today().replace(day=1) - timedelta(days=1)
    start = (cutoff.replace(day=1) - timedelta(days=30 * (months - 1))).replace(day=1)
    
    # Get average spending per category
    rows = db.session.query(
        Category.id, Category.name,
        func.sum(Transaction.amount), func.count(func.distinct(func.date_trunc('month', Transaction.date)))
    ).join(Category).filter(
        Transaction.date >= start,
        Transaction.date <= cutoff,
        Transaction.is_deleted == False
    ).group_by(Category.id, Category.name).all()
    
    count = 0
    for cat_id, cat_name, total, month_count in rows:
        if cat_name == 'Uncategorized' or cat_name == EXCLUDED_CAT:
            continue
        avg = abs(float(total)) / max(int(month_count), 1)
        if avg < 1:
            continue
        item_type = 'income' if total > 0 else 'expense'
        existing = BudgetLineItem.query.filter_by(budget_id=plan_id, category_id=cat_id).first()
        if not existing:
            item = BudgetLineItem(budget_id=plan_id, label=cat_name, item_type=item_type,
                                  category_id=cat_id, expected_amount=round(avg, 2))
            db.session.add(item)
            count += 1
    db.session.commit()
    return jsonify({'success': True, 'message': f'Added {count} line items from {months}-month averages.'})

@app.route('/api/budget_vs_actual')
def api_budget_vs_actual():
    year = int(request.args.get('year', date.today().year))
    month = int(request.args.get('month', date.today().month))
    
    active = BudgetPlan.query.filter_by(is_active=True).first()
    if not active:
        return jsonify({'success': False, 'message': 'No active budget plan.'})
    
    month_start = date(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    month_end = date(year, month, days_in_month)
    today = date.today()
    days_elapsed = min((today - month_start).days + 1, days_in_month) if today >= month_start else 0
    if today > month_end:
        days_elapsed = days_in_month
    pct_elapsed = round(days_elapsed / days_in_month * 100)
    
    line_items_raw = BudgetLineItem.query.filter_by(budget_id=active.id).all()
    
    # Sort line items: entity-specific first (more specific), then category-only (more general)
    # This prevents broad categories from stealing transactions from specific entities
    line_items = sorted(line_items_raw, key=lambda x: (0 if x.entity_id else 1, x.label))
    
    # Determine date ranges based on frequency
    # For annual items: year-to-date, for quarterly: quarter-to-date, for monthly: current month
    year_start = date(year, 1, 1)
    quarter = (month - 1) // 3 + 1
    quarter_start = date(year, (quarter - 1) * 3 + 1, 1)
    
    # Get all transactions for finding most recent payments (annual/quarterly need history)
    # No date filter here - we need to search all time for the most recent payment
    # Exclude 'Ignored Credit Card Payment' category (Chase/Wells Fargo payment confirmations)
    all_txs = Transaction.query.join(Category).join(Entity).filter(
        Transaction.is_deleted == False,
        Category.name != 'Ignored Credit Card Payment'
    ).all()
    
    # Build results per line item
    items_result = []
    matched_tx_ids = set()
    total_expected_income = 0
    total_expected_expense = 0
    total_actual_income = 0
    total_actual_expense = 0
    
    for li in line_items:
        # Normalize expected amount based on frequency
        raw_expected = float(li.expected_amount)
        frequency = getattr(li, 'frequency', 'monthly')  # Default to monthly for backward compatibility
        if frequency == 'annual':
            expected_monthly = raw_expected / 12
        elif frequency == 'quarterly':
            expected_monthly = raw_expected / 3
        else:  # monthly
            expected_monthly = raw_expected
        
        if li.item_type == 'income':
            total_expected_income += expected_monthly
        else:
            total_expected_expense += expected_monthly
        
        # For annual/quarterly items: find the most recent transaction (any time)
        # For monthly items: sum transactions for the current month
        actual = 0.0
        last_payment_amount = 0.0
        
        if frequency in ['annual', 'quarterly']:
            # Find most recent transaction for this item (not limited to current period)
            most_recent_tx = None
            for t in all_txs:
                # Skip if already matched by a previous budget item
                if t.id in matched_tx_ids:
                    continue
                
                matched = False
                # If entity is specified, only match by entity
                if li.entity_id:
                    if t.entity_id == li.entity_id:
                        matched = True
                # Otherwise, match by category
                elif li.category_id and t.category_id == li.category_id:
                    matched = True
                
                if matched:
                    if most_recent_tx is None or t.date > most_recent_tx.date:
                        most_recent_tx = t
            
            if most_recent_tx:
                last_payment_amount = abs(float(most_recent_tx.amount))
                matched_tx_ids.add(most_recent_tx.id)
                actual = last_payment_amount
        else:
            # Monthly: sum all transactions in the current month
            for t in all_txs:
                # Filter to current month only
                if t.date < month_start or t.date > month_end:
                    continue
                    
                # Skip if already matched by a previous budget item
                if t.id in matched_tx_ids:
                    continue
                
                matched = False
                # If entity is specified, only match by entity
                if li.entity_id:
                    if t.entity_id == li.entity_id:
                        matched = True
                # Otherwise, match by category
                elif li.category_id and t.category_id == li.category_id:
                    matched = True
                
                if matched:
                    actual += abs(float(t.amount))
                    matched_tx_ids.add(t.id)
        
        if li.item_type == 'income':
            total_actual_income += actual
        else:
            total_actual_expense += actual
        
        # Normalize actual to monthly equivalent for display
        # Annual: divide by 12, Quarterly: divide by 3, Monthly: as-is
        if frequency == 'annual':
            actual_display = actual / 12
        elif frequency == 'quarterly':
            actual_display = actual / 3
        else:  # monthly
            actual_display = actual
        
        # Variance and status compare normalized monthly amounts
        variance = actual_display - expected_monthly
        pct_var = round(variance / expected_monthly * 100) if expected_monthly else 0
        
        # Project end-of-period
        if frequency == 'annual':
            # Project YTD to full year
            if month > 0:
                projected = actual / month * 12
            else:
                projected = 0
        elif frequency == 'quarterly':
            # Project QTD to full quarter
            months_in_quarter = ((month - 1) % 3) + 1
            if months_in_quarter > 0:
                projected = actual / months_in_quarter * 3
            else:
                projected = 0
        else:  # monthly
            # Project to end-of-month
            if days_elapsed > 0:
                projected = actual / days_elapsed * days_in_month
            else:
                projected = 0
        
        # Status: green=good, yellow=warning, red=bad (based on variance)
        if expected_monthly > 0:
            if li.item_type == 'income':
                # Income: positive variance = green (earning more), negative = red (earning less)
                if variance >= 0:
                    status = 'green'
                elif variance >= -expected_monthly * 0.25:  # Within 25% of expected
                    status = 'yellow'
                else:
                    status = 'red'
            else:
                # Expense: negative variance = green (under budget), positive = red (over budget)
                if variance <= 0:
                    status = 'green'
                elif variance <= expected_monthly * 0.1:  # Within 10% over budget
                    status = 'yellow'
                else:
                    status = 'red'
        else:
            status = 'neutral'
        
        items_result.append({
            'label': li.label,
            'item_type': li.item_type,
            'notes': li.notes,
            'frequency': frequency,
            'expected_raw': round(raw_expected, 2),
            'expected': round(expected_monthly, 2),
            'actual': round(actual_display, 2),
            'variance': round(variance, 2),
            'pct_variance': pct_var,
            'projected': round(projected, 2),
            'status': status
        })
    
    # Unbudgeted transactions (only for current month)
    unbudgeted = []
    for t in all_txs:
        if t.date >= month_start and t.date <= month_end and t.id not in matched_tx_ids and t.category.name != EXCLUDED_CAT:
            unbudgeted.append({
                'date': t.date.strftime('%Y-%m-%d'),
                'entity': t.entity.name,
                'category': t.category.name,
                'amount': float(t.amount)
            })
    
    return jsonify({
        'success': True,
        'month_name': calendar.month_name[month],
        'year': year,
        'days_elapsed': days_elapsed,
        'days_in_month': days_in_month,
        'pct_elapsed': pct_elapsed,
        'totals': {
            'expected_income': round(total_expected_income, 2),
            'actual_income': round(total_actual_income, 2),
            'expected_expense': round(total_expected_expense, 2),
            'actual_expense': round(total_actual_expense, 2),
            'expected_surplus': round(total_expected_income - total_expected_expense, 2),
            'actual_surplus': round(total_actual_income - total_actual_expense, 2)
        },
        'items': items_result,
        'unbudgeted': unbudgeted
    })

@app.route('/edit_accounts')
def edit_accounts():
    accs = Account.query.order_by(Account.name).all()
    return render_template('edit_account_balances.html', accounts=accs, account_types=['checking','savings','credit_card','brokerage'])

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
    
    # Get transaction count for each category
    tx_counts = dict(db.session.query(
        Transaction.category_id,
        func.count(Transaction.id)
    ).filter(Transaction.is_deleted == False).group_by(Transaction.category_id).all())
    
    categories = Category.query.order_by(Category.name).all()
    
    # Add transaction count to each category object
    for cat in categories:
        cat.transaction_count = tx_counts.get(cat.id, 0)
    
    return render_template('manage_categories.html', categories=categories)

@app.route('/manage_categories/delete/<int:cat_id>', methods=['POST'])
def delete_category(cat_id):
    uncat = get_uncategorized_id()
    if cat_id != uncat:
        Transaction.query.filter_by(category_id=cat_id).update({'category_id': uncat})
        Entity.query.filter_by(category_id=cat_id).update({'category_id': uncat})
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

@app.route('/api/trend_data', methods=['POST'])
def get_trend_data():
    data = request.get_json()
    cat_ids = data.get('category_ids', [])
    payee_names = data.get('payee_names', [])
    acct_ids = [int(i) for i in data.get('account_ids', [])]
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
    
    # DEFINE BUCKET COLUMN WITH SUNDAY SHIFT
    if bucket == 'week':
        # Shift date forward 1 day so Sunday becomes Monday (start of standard week), 
        # truncate, then shift back 1 day.
        bucket_col = func.date_trunc('week', Transaction.date + text("INTERVAL '1 DAY'")) - text("INTERVAL '1 DAY'")
    else:
        bucket_col = func.date_trunc(bucket, Transaction.date)
    
    if cat_ids:
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
        query = db.session.query(bucket_col.label('bucket_start'), Entity.name.label('name'), func.sum(Transaction.amount).label('total')).select_from(Transaction).join(Entity).filter(Transaction.date >= s_date, Transaction.date <= e_date, Transaction.is_deleted == False, Entity.name.in_(payee_names))
        
        # FIX: Apply Account ID filter
        if acct_ids:
            query = query.filter(Transaction.account_id.in_(acct_ids))
            
        query = query.group_by(bucket_col, Entity.name).order_by(bucket_col)
        for row in query.all():
            b_date = row.bucket_start.strftime('%Y-%m-%d')
            name = f"[P] {row.name}"
            if name not in data_by_item: data_by_item[name] = {}
            data_by_item[name][b_date] = data_by_item[name].get(b_date, 0) + abs(float(row.total))

    plot_data = []
    for name, pts in data_by_item.items():
        y_vals = [pts.get(b, 0) for b in sorted_buckets]
        plot_data.append({'type': chart_type, 'name': name, 'x': sorted_buckets, 'y': y_vals, 'mode': chart_mode, 'fill': fill_type})

    events = Event.query.filter(Event.date >= s_date, Event.date <= e_date, Event.id.in_(evt_ids)).all()
    shapes = [{'type': 'line', 'x0': e.date.strftime('%Y-%m-%d'), 'x1': e.date.strftime('%Y-%m-%d'), 'y0':0, 'y1':1, 'yref':'paper', 'line': {'color':'pink', 'dash':'dot'}} for e in events]
    anns = [{'x': e.date.strftime('%Y-%m-%d'), 'y':0.95, 'yref':'paper', 'text': e.description, 'showarrow':False, 'bgcolor':'rgba(255,192,203,0.8)'} for e in events]
    return jsonify({'plot_data': plot_data, 'layout_shapes': shapes, 'layout_annotations': anns})

@app.route('/api/yoy_comparison', methods=['POST'])
def api_yoy_comparison():
    """Year-over-year comparison: same categories/payees overlaid by year."""
    try:
        data = request.get_json()
        cat_ids = data.get('category_ids', [])
        payee_names = data.get('payee_names', [])
        acct_ids = [int(i) for i in data.get('account_ids', [])]
        years = data.get('years', [])
        if not years:
            today = date.today()
            years = [today.year - 2, today.year - 1, today.year]
        years = [int(y) for y in years]
        
        month_labels = list(calendar.month_abbr[1:])
        colors = ['#9ca3af', '#f59e0b', '#6366f1', '#10b981', '#ef4444']
        
        plot_data = []
        
        for yi, yr in enumerate(sorted(years)):
            yr_start = date(yr, 1, 1)
            yr_end = date(yr, 12, 31)
            monthly = {m: 0.0 for m in range(1, 13)}
            
            query = Transaction.query.join(Category).join(Entity).filter(
                Transaction.date >= yr_start,
                Transaction.date <= yr_end,
                Transaction.is_deleted == False
            )
            if acct_ids:
                query = query.filter(Transaction.account_id.in_(acct_ids))
            
            filters = []
            if cat_ids:
                filters.append(Transaction.category_id.in_(cat_ids))
            if payee_names:
                filters.append(Entity.name.in_(payee_names))
            if filters:
                from sqlalchemy import or_
                query = query.filter(or_(*filters))
            else:
                continue
            
            for t in query.all():
                monthly[t.date.month] += abs(float(t.amount))
            
            y_vals = [monthly[m] for m in range(1, 13)]
            color = colors[yi % len(colors)]
            plot_data.append({
                'type': 'bar', 'name': str(yr), 'x': month_labels, 'y': y_vals,
                'marker': {'color': color},
                'text': [f'${v:,.0f}' for v in y_vals],
                'textposition': 'auto'
            })
        
        return jsonify({'plot_data': plot_data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/spending_velocity', methods=['POST'])
def api_spending_velocity():
    """Spending velocity: cumulative daily spend for current vs prior months, with projection."""
    try:
        data = request.get_json()
        cat_ids = data.get('category_ids', [])
        payee_names = data.get('payee_names', [])
        acct_ids = [int(i) for i in data.get('account_ids', [])]
        
        today = date.today()
        cm_start = today.replace(day=1)
        cm_end = today
        cm_days_total = calendar.monthrange(today.year, today.month)[1]
        
        pm_end = cm_start - timedelta(days=1)
        pm_start = pm_end.replace(day=1)
        pm_days_total = calendar.monthrange(pm_start.year, pm_start.month)[1]
        
        def get_daily_cumulative(start, end, days_total):
            query = Transaction.query.join(Category).join(Entity).filter(
                Transaction.date >= start,
                Transaction.date <= end,
                Transaction.is_deleted == False,
                Transaction.amount < 0,
                Category.name != EXCLUDED_CAT
            )
            if acct_ids:
                query = query.filter(Transaction.account_id.in_(acct_ids))
            filters = []
            if cat_ids:
                filters.append(Transaction.category_id.in_(cat_ids))
            if payee_names:
                filters.append(Entity.name.in_(payee_names))
            if filters:
                from sqlalchemy import or_
                query = query.filter(or_(*filters))
            
            daily = {d: 0.0 for d in range(1, days_total + 1)}
            for t in query.all():
                daily[t.date.day] += abs(float(t.amount))
            
            cumulative = []
            running = 0.0
            for d in range(1, days_total + 1):
                running += daily[d]
                cumulative.append(running)
            return cumulative
        
        cm_cum = get_daily_cumulative(cm_start, cm_end, cm_days_total)
        pm_cum = get_daily_cumulative(pm_start, pm_end, pm_days_total)
        
        days_elapsed = (cm_end - cm_start).days + 1
        if days_elapsed > 0 and cm_cum:
            daily_rate = cm_cum[days_elapsed - 1] / days_elapsed
            projected = []
            for d in range(1, cm_days_total + 1):
                if d <= days_elapsed:
                    projected.append(cm_cum[d - 1])
                else:
                    projected.append(cm_cum[days_elapsed - 1] + daily_rate * (d - days_elapsed))
        else:
            daily_rate = 0
            projected = [0] * cm_days_total
        
        cm_label = cm_start.strftime('%B %Y')
        pm_label = pm_start.strftime('%B %Y')
        x_days = list(range(1, max(cm_days_total, pm_days_total) + 1))
        
        plot_data = [
            {
                'type': 'scatter', 'mode': 'lines+markers',
                'name': pm_label, 'x': list(range(1, pm_days_total + 1)), 'y': pm_cum,
                'line': {'color': '#9ca3af', 'width': 2}
            },
            {
                'type': 'scatter', 'mode': 'lines+markers',
                'name': cm_label, 'x': list(range(1, days_elapsed + 1)), 'y': cm_cum[:days_elapsed],
                'line': {'color': '#6366f1', 'width': 3}
            },
            {
                'type': 'scatter', 'mode': 'lines',
                'name': 'Projected', 'x': list(range(days_elapsed, cm_days_total + 1)),
                'y': projected[days_elapsed - 1:],
                'line': {'color': '#6366f1', 'width': 2, 'dash': 'dot'}
            }
        ]
        
        summary = {
            'daily_rate': round(daily_rate, 2),
            'projected_total': round(projected[-1], 2) if projected else 0,
            'prior_month_total': round(pm_cum[-1], 2) if pm_cum else 0,
            'current_spent': round(cm_cum[days_elapsed - 1], 2) if cm_cum and days_elapsed > 0 else 0,
            'days_elapsed': days_elapsed,
            'days_remaining': cm_days_total - days_elapsed
        }
        
        return jsonify({'plot_data': plot_data, 'summary': summary, 'x_days': x_days})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/min_max_dates')
def api_date_range():
    min_date = db.session.query(func.min(Transaction.date)).scalar()
    max_date = db.session.query(func.max(Transaction.date)).scalar()
    return jsonify({'min_date': min_date.strftime('%Y-%m-%d') if min_date else date.today().strftime('%Y-%m-%d'), 'max_date': max_date.strftime('%Y-%m-%d') if max_date else date.today().strftime('%Y-%m-%d')})

if __name__ == '__main__':
    with app.app_context(): create_tables()
    app.run(host='0.0.0.0', port=5001, debug=True)
