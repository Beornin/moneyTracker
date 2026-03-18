import calendar
from datetime import datetime, date, timedelta
from models import db, Category, Entity, Transaction

# Constants
DASHBOARD_MONTH_SPAN = 12
EXCLUDED_CAT = 'Ignored Credit Card Payment'
EXCLUDED_CAT_CORE = ['Car Payment', 'VUL', 'AC Payment', 'Taxes']
EXCLUDED_PAYEE_LABELS_CORE = [
    'Planting Oaks', 'Jaxco Furniture', 'Planting Oaks', 'Jiu Jitsu', 'Abeka', 'Christianbook',
    'New Leaf Publishing', 'Veritas', 'Sp Goodandbeautiful Goodandbeauti Ut',
    'Simplify Health', 'Fullscript', 'Kbmo Diagnostics', 'Rupa Labs'
]

def get_uncategorized_id():
    return Category.query.filter_by(name='Uncategorized').first().id

def try_parse_date(date_str):
    if not date_str: return None
    date_str = date_str.strip()
    
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        pass

    clean_date_str = date_str.replace('.', '/').replace('-', '/')
    for fmt in ('%m/%d/%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(clean_date_str, fmt).date()
        except ValueError:
            continue
    return None

def find_or_create_entity(description, amount, uncat_id):
    """
    Smart entity matching with auto-creation.
    Returns: (entity, category_id)
    """
    desc_upper = description.upper().strip()
    clean_name = description.title()
    
    entities = Entity.query.filter(Entity.match_patterns != None).all()
    
    for entity in entities:
        if entity.match_patterns:
            for pattern in entity.match_patterns:
                if pattern.upper() in desc_upper:
                    if entity.match_type == 'positive' and amount <= 0:
                        continue
                    if entity.match_type == 'negative' and amount >= 0:
                        continue
                    return entity, entity.category_id
    
    entity = Entity.query.filter_by(name=clean_name).first()
    if entity:
        return entity, entity.category_id
    
    new_entity = Entity(
        name=clean_name,
        category_id=uncat_id,
        match_patterns=[],
        is_auto_created=True
    )
    db.session.add(new_entity)
    db.session.commit()
    
    return new_entity, uncat_id

def update_entity_patterns(entity_id, patterns, category_id, match_type='any'):
    """Update an entity's match patterns and category."""
    entity = db.session.get(Entity, entity_id)
    if entity:
        entity.match_patterns = patterns if isinstance(patterns, list) else [patterns]
        entity.category_id = category_id
        entity.match_type = match_type
        entity.is_auto_created = False
        db.session.commit()
        return True
    return False

def apply_entity_to_transactions(entity_id, category_id):
    """Apply entity's category to all its transactions."""
    count = Transaction.query.filter_by(entity_id=entity_id).update(
        {'category_id': category_id}, 
        synchronize_session=False
    )
    db.session.commit()
    return count

def rematch_all_entities():
    """Re-match all transactions using current entity patterns."""
    uncat_id = get_uncategorized_id()
    all_txs = Transaction.query.all()
    updated = 0
    
    for t in all_txs:
        entity, cat_id = find_or_create_entity(t.original_description, t.amount, uncat_id)
        if t.entity_id != entity.id or t.category_id != cat_id:
            t.entity_id = entity.id
            t.category_id = cat_id
            updated += 1
    
    db.session.commit()
    return updated

def get_monthly_summary_direct(month_offset):
    today = date.today()
    idx = today.month - 1 + month_offset
    year, month = today.year + (idx // 12), (idx % 12) + 1
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    current_month_name = start.strftime('%B %Y')
    return {'start_date': start, 'end_date': end, 'current_month_name': current_month_name}

def get_spending_data_for_period(start, end):
    """Helper for AI insights - excludes HSA accounts."""
    from models import Account, Category, Transaction
    
    hsa_subquery = db.session.query(Account.id).filter(Account.account_type == 'hsa')
    
    transactions = Transaction.query.join(Category).filter(
        Transaction.date >= start, 
        Transaction.date <= end, 
        Transaction.is_deleted == False,
        Transaction.account_id.notin_(hsa_subquery)
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
