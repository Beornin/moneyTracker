import calendar
from datetime import datetime, date
from models import db, Category, Entity, Transaction


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


def auto_match_transactions_to_entity(entity):
    """
    Auto-match unassigned transactions to this entity based on match patterns.
    Returns count of reassigned transactions.
    """
    if not entity.match_patterns:
        return 0

    auto_entity_ids = [e.id for e in Entity.query.filter_by(is_auto_created=True).all()]

    if not auto_entity_ids:
        return 0

    unassigned_txs = Transaction.query.filter(
        Transaction.entity_id.in_(auto_entity_ids),
        Transaction.is_deleted == False
    ).all()

    matched_count = 0
    for tx in unassigned_txs:
        desc_upper = tx.original_description.upper().strip()

        for pattern in entity.match_patterns:
            if pattern.upper() in desc_upper:
                tx.entity_id = entity.id
                tx.category_id = entity.category_id
                matched_count += 1
                break

    if matched_count > 0:
        db.session.commit()

    return matched_count


def rematch_all_entities():
    """Re-run entity matching on all auto-created entities."""
    auto_entities = Entity.query.filter_by(is_auto_created=True).all()

    rule_entities = Entity.query.filter(
        Entity.is_auto_created == False,
        Entity.match_patterns != None
    ).all()

    updated = 0
    for auto_entity in auto_entities:
        name_upper = auto_entity.name.upper()
        matched = False

        for rule_entity in rule_entities:
            if rule_entity.match_patterns:
                for pattern in rule_entity.match_patterns:
                    if pattern.upper() in name_upper:
                        Transaction.query.filter_by(entity_id=auto_entity.id).update(
                            {'entity_id': rule_entity.id, 'category_id': rule_entity.category_id},
                            synchronize_session=False
                        )
                        db.session.delete(auto_entity)
                        updated += 1
                        matched = True
                        break
                if matched: break

    db.session.commit()
    return updated


def get_monthly_summary_direct(month_offset):
    today = date.today()
    idx = today.month - 1 + month_offset
    year, month = today.year + (idx // 12), (idx % 12) + 1
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    current_month_name = start.strftime('%B %Y')
    return {'start_date': start, 'end_date': end, 'current_month_name': current_month_name}
