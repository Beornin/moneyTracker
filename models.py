from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from sqlalchemy.orm import joinedload

db = SQLAlchemy()

class TimestampMixin(object):
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

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
    end_date = db.Column(db.Date, nullable=False, index=True)

    account = db.relationship('Account', backref='statement_records', lazy=True)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'start_date', 'end_date', name='_statement_period_uc'),
        # Composite for dashboard max(end_date) queries filtered by account
        db.Index('idx_sr_account_end', 'account_id', 'end_date'),
    )

class Category(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    type = db.Column(db.String(20), nullable=False)
    transactions = db.relationship('Transaction', backref='category', lazy=True)
    entities = db.relationship('Entity', backref='category', lazy=True)

class Entity(db.Model, TimestampMixin):
    """Simplified entity model combining Payee + PayeeRule functionality."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    match_patterns = db.Column(db.JSON, default=list)
    match_type = db.Column(db.String(20), default='any', nullable=False)
    is_auto_created = db.Column(db.Boolean, default=False, nullable=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    transactions = db.relationship('Transaction', backref='entity', lazy=True)

    __table_args__ = (
        db.Index('idx_entity_category', 'category_id'),
    )

class Transaction(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    original_description = db.Column(db.String(500), nullable=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    entity_id = db.Column(db.Integer, db.ForeignKey('entity.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    __table_args__ = (
        db.Index('idx_tx_date_deleted', 'date', 'is_deleted'),
        db.Index('idx_tx_category', 'category_id'),
        db.Index('idx_tx_account', 'account_id'),
        db.Index('idx_tx_entity', 'entity_id'),
        db.Index('idx_tx_account_date_amount', 'account_id', 'date', 'amount'),
    )

class Event(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    description = db.Column(db.String(500), nullable=False)
    __table_args__ = (db.UniqueConstraint('date', 'description', name='_event_uc'),)

class Budget(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    criteria = db.Column(db.Text, nullable=False)

class BudgetPlan(db.Model, TimestampMixin):
    """A named budget plan containing expected monthly amounts."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False, index=True)
    line_items = db.relationship('BudgetLineItem', backref='budget_plan', lazy=True, cascade='all, delete-orphan')

class BudgetLineItem(db.Model, TimestampMixin):
    """A single line item in a budget plan with an expected amount and frequency."""
    id = db.Column(db.Integer, primary_key=True)
    budget_id = db.Column(db.Integer, db.ForeignKey('budget_plan.id'), nullable=False)
    label = db.Column(db.String(200), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    entity_id = db.Column(db.Integer, db.ForeignKey('entity.id'), nullable=True)
    expected_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    item_type = db.Column(db.String(20), nullable=False, default='expense')
    frequency = db.Column(db.String(20), nullable=False, default='monthly')
    notes = db.Column(db.Text, nullable=True)

    category = db.relationship('Category', lazy=True)
    entity = db.relationship('Entity', lazy=True)

    __table_args__ = (
        db.Index('idx_bli_budget', 'budget_id'),
    )

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

def get_active_budget():
    """Returns the active BudgetPlan with line_items eagerly loaded, or None."""
    return BudgetPlan.query.options(
        joinedload(BudgetPlan.line_items).joinedload(BudgetLineItem.category),
        joinedload(BudgetPlan.line_items).joinedload(BudgetLineItem.entity),
    ).filter_by(is_active=True).first()

def get_budget_core_filters(budget):
    """Extract sorted line items from budget for core filtering.
    Returns sorted line items (entity-specific first) or None if no budget.
    """
    if not budget:
        return None
    return sorted(budget.line_items, key=lambda x: (0 if x.entity_id else 1, x.label))

def is_transaction_budgeted(t, budgeted_line_items):
    """Check if a transaction matches any budget line item.
    Entity-specific items take precedence to prevent double-counting.
    """
    if not budgeted_line_items:
        return False
    for li in budgeted_line_items:
        if li.entity_id:
            if t.entity_id == li.entity_id:
                return True
        elif li.category_id and t.category_id == li.category_id:
            return True
    return False
