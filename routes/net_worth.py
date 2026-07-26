"""
Net Worth / Assets & Debts tracker — Flask Blueprint.
All routes under /net_worth/*. Fully standalone: never reads/writes
Account or Transaction — balances are manual, dated snapshots.
"""
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash

from constants import NET_WORTH_ASSET_CATEGORIES, NET_WORTH_DEBT_CATEGORIES, NET_WORTH_LIQUIDITY_HORIZONS
from models import db, NetWorthAccount, NetWorthSnapshot
from services.net_worth import NetWorthService

net_worth_bp = Blueprint('net_worth', __name__, url_prefix='/net_worth')


# ── Index ───────────────────────────────────────────────────────────────

@net_worth_bp.route('/')
def index():
    service = NetWorthService()
    summary = service.get_summary()
    owners = sorted({a.owner for a in NetWorthAccount.query.all()})
    return render_template(
        'net_worth/index.html',
        summary=summary,
        owners=owners,
        asset_categories=NET_WORTH_ASSET_CATEGORIES,
        debt_categories=NET_WORTH_DEBT_CATEGORIES,
        horizons=NET_WORTH_LIQUIDITY_HORIZONS,
        chart_net_worth=service.get_net_worth_chart(),
        chart_horizon=service.get_horizon_chart(),
        chart_category=service.get_category_chart(),
    )


# ── Account CRUD ────────────────────────────────────────────────────────

@net_worth_bp.route('/account/add', methods=['POST'])
def add_account():
    item_type = request.form.get('item_type', 'asset')
    name = request.form.get('name', '').strip()
    owner = request.form.get('owner', '').strip()
    category = request.form.get('category', '').strip()
    balance_str = request.form.get('balance', '').strip()
    as_of_str = request.form.get('as_of_date', '').strip()

    if not (name and owner and category and balance_str):
        flash("Name, owner, category, and a starting balance are required.", "danger")
        return redirect(url_for('net_worth.index'))

    try:
        balance = float(balance_str)
    except ValueError:
        flash("Starting balance must be a number.", "danger")
        return redirect(url_for('net_worth.index'))

    as_of = date.fromisoformat(as_of_str) if as_of_str else date.today()

    account = NetWorthAccount(
        owner=owner,
        name=name,
        item_type=item_type,
        category=category,
        institution=request.form.get('institution', '').strip() or None,
        account_number=request.form.get('account_number', '').strip() or None,
        online_access_url=request.form.get('online_access_url', '').strip() or None,
        liquidity_horizon=request.form.get('liquidity_horizon') or None,
        notes=request.form.get('notes', '').strip() or None,
        include_in_net_worth=request.form.get('include_in_net_worth') == 'on',
    )
    db.session.add(account)
    db.session.flush()
    db.session.add(NetWorthSnapshot(net_worth_account_id=account.id, date=as_of, balance=balance))
    db.session.commit()
    flash(f"Added '{name}'.", "success")
    return redirect(url_for('net_worth.index'))


@net_worth_bp.route('/account/<int:account_id>/update', methods=['POST'])
def update_account(account_id):
    account = db.session.get(NetWorthAccount, account_id)
    if not account:
        flash("Account not found.", "danger")
        return redirect(url_for('net_worth.index'))

    # Only touch a field if the submitting form actually included it, so a
    # partial edit form (e.g. the compact inline row) can't silently null out
    # fields it doesn't render (institution, notes, account number, etc.).
    f = request.form
    if 'owner' in f and f['owner'].strip():
        account.owner = f['owner'].strip()
    if 'name' in f and f['name'].strip():
        account.name = f['name'].strip()
    if 'category' in f and f['category'].strip():
        account.category = f['category'].strip()
    if 'institution' in f:
        account.institution = f['institution'].strip() or None
    if 'account_number' in f:
        account.account_number = f['account_number'].strip() or None
    if 'online_access_url' in f:
        account.online_access_url = f['online_access_url'].strip() or None
    if 'liquidity_horizon' in f:
        account.liquidity_horizon = f['liquidity_horizon'] or None
    if 'notes' in f:
        account.notes = f['notes'].strip() or None
    if 'include_in_net_worth_present' in f:
        account.include_in_net_worth = 'include_in_net_worth' in f
    db.session.commit()
    flash(f"Updated '{account.name}'.", "success")
    return redirect(url_for('net_worth.index'))


@net_worth_bp.route('/account/<int:account_id>/archive', methods=['POST'])
def archive_account(account_id):
    account = db.session.get(NetWorthAccount, account_id)
    if account:
        account.is_archived = True
        db.session.commit()
        flash(f"Archived '{account.name}'.", "success")
    return redirect(url_for('net_worth.index'))


@net_worth_bp.route('/account/<int:account_id>/unarchive', methods=['POST'])
def unarchive_account(account_id):
    account = db.session.get(NetWorthAccount, account_id)
    if account:
        account.is_archived = False
        db.session.commit()
        flash(f"Restored '{account.name}'.", "success")
    return redirect(url_for('net_worth.index'))


@net_worth_bp.route('/account/<int:account_id>/delete', methods=['POST'])
def delete_account(account_id):
    account = db.session.get(NetWorthAccount, account_id)
    if account:
        name = account.name
        db.session.delete(account)
        db.session.commit()
        flash(f"Deleted '{name}'.", "success")
    return redirect(url_for('net_worth.index'))


@net_worth_bp.route('/account/<int:account_id>/history')
def account_history(account_id):
    account = db.session.get(NetWorthAccount, account_id)
    if not account:
        flash("Account not found.", "danger")
        return redirect(url_for('net_worth.index'))
    snapshots = sorted(account.snapshots, key=lambda s: s.date, reverse=True)
    return render_template('net_worth/history.html', account=account, snapshots=snapshots,
                            today=date.today().isoformat())


# ── Snapshot CRUD (single, ad-hoc) ──────────────────────────────────────

@net_worth_bp.route('/snapshot/add', methods=['POST'])
def add_snapshot():
    account_id = request.form.get('net_worth_account_id', type=int)
    balance_str = request.form.get('balance', '').strip()
    date_str = request.form.get('date', '').strip()

    account = db.session.get(NetWorthAccount, account_id) if account_id else None
    if not account:
        flash("Account not found.", "danger")
        return redirect(url_for('net_worth.index'))

    try:
        balance = float(balance_str)
        snap_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        flash("Enter a valid balance and date.", "danger")
        return redirect(url_for('net_worth.account_history', account_id=account_id))

    db.session.add(NetWorthSnapshot(net_worth_account_id=account_id, date=snap_date, balance=balance))
    db.session.commit()
    flash(f"Recorded balance for '{account.name}'.", "success")
    return redirect(url_for('net_worth.account_history', account_id=account_id))


@net_worth_bp.route('/snapshot/<int:snap_id>/delete', methods=['POST'])
def delete_snapshot(snap_id):
    snap = db.session.get(NetWorthSnapshot, snap_id)
    if snap:
        account_id = snap.net_worth_account_id
        db.session.delete(snap)
        db.session.commit()
        flash("Snapshot deleted.", "success")
        return redirect(url_for('net_worth.account_history', account_id=account_id))
    return redirect(url_for('net_worth.index'))


# ── Batch Checkup ────────────────────────────────────────────────────────

@net_worth_bp.route('/checkup', methods=['GET', 'POST'])
def checkup():
    service = NetWorthService()

    if request.method == 'POST':
        as_of_str = request.form.get('as_of_date', '').strip()
        try:
            as_of = date.fromisoformat(as_of_str) if as_of_str else date.today()
        except ValueError:
            flash("Enter a valid date.", "danger")
            return redirect(url_for('net_worth.checkup'))

        updated = 0
        for key, val in request.form.items():
            if not key.startswith('balance_') or not val.strip():
                continue
            try:
                account_id = int(key[len('balance_'):])
                balance = float(val)
            except ValueError:
                continue
            db.session.add(NetWorthSnapshot(net_worth_account_id=account_id, date=as_of, balance=balance))
            updated += 1

        db.session.commit()
        flash(f"Recorded {updated} balance update(s) as of {as_of}.", "success")
        return redirect(url_for('net_worth.index'))

    accounts = [a for a in service.accounts]
    accounts.sort(key=lambda a: (a.owner, a.item_type, a.name))
    last_balances = {a.id: service.current_balance(a) for a in accounts}
    return render_template('net_worth/checkup.html', accounts=accounts, last_balances=last_balances,
                            today=date.today().isoformat())
