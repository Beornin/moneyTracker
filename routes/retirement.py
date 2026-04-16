"""
Retirement planner routes — Flask Blueprint.
All routes under /retirement/*.
"""
import json
from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy.orm import joinedload

from models import (
    db, Category, RetirementScenario, RetirementPerson, RetirementAccount,
    RetirementIncomeSource, RetirementExpenseItem, PortfolioSnapshot,
)
from services.retirement import RetirementService, get_expense_averages_by_category

retirement_bp = Blueprint('retirement', __name__, url_prefix='/retirement')


# ── Scenario List ──────────────────────────────────────────────────────

@retirement_bp.route('/')
def index():
    scenarios = RetirementScenario.query.order_by(RetirementScenario.name).all()
    return render_template('retirement/index.html', scenarios=scenarios)


# ── Create Scenario ────────────────────────────────────────────────────

@retirement_bp.route('/scenario/new', methods=['GET', 'POST'])
def new_scenario():
    if request.method == 'POST':
        try:
            sc = RetirementScenario(
                name=request.form.get('name', 'New Scenario').strip(),
                filing_status=request.form.get('filing_status', 'mfj'),
                life_expectancy_age=int(request.form.get('life_expectancy_age', 90)),
                growth_rate_mean=float(request.form.get('growth_rate_mean', 7)) / 100,
                growth_rate_stddev=float(request.form.get('growth_rate_stddev', 15)) / 100,
                inflation_rate=float(request.form.get('inflation_rate', 3)) / 100,
                notes=request.form.get('notes', '').strip() or None,
            )
            db.session.add(sc)
            db.session.flush()

            # Add person 1
            p1 = RetirementPerson(
                scenario_id=sc.id,
                label=request.form.get('person1_label', 'Person 1').strip(),
                date_of_birth=datetime.strptime(request.form['person1_dob'], '%Y-%m-%d').date(),
                retirement_age=int(request.form.get('person1_retire_age', 65)),
                ss_monthly_benefit=float(request.form.get('person1_ss', 0)),
                ss_start_age=int(request.form.get('person1_ss_age', 67)),
            )
            db.session.add(p1)

            # Add person 2 if provided
            if request.form.get('person2_label', '').strip():
                p2 = RetirementPerson(
                    scenario_id=sc.id,
                    label=request.form.get('person2_label').strip(),
                    date_of_birth=datetime.strptime(request.form['person2_dob'], '%Y-%m-%d').date(),
                    retirement_age=int(request.form.get('person2_retire_age', 65)),
                    ss_monthly_benefit=float(request.form.get('person2_ss', 0)),
                    ss_start_age=int(request.form.get('person2_ss_age', 67)),
                )
                db.session.add(p2)

            db.session.commit()
            flash(f"Created scenario '{sc.name}'.", "success")
            return redirect(url_for('retirement.edit_scenario', scenario_id=sc.id))
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating scenario: {e}", "danger")

    return render_template('retirement/scenario_form.html', scenario=None, categories=Category.query.order_by(Category.name).all())


# ── Edit Scenario ──────────────────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>', methods=['GET', 'POST'])
def edit_scenario(scenario_id):
    sc = RetirementScenario.query.options(
        joinedload(RetirementScenario.people).joinedload(RetirementPerson.accounts),
        joinedload(RetirementScenario.income_sources),
        joinedload(RetirementScenario.expense_items).joinedload(RetirementExpenseItem.category),
    ).get_or_404(scenario_id)

    if request.method == 'POST':
        try:
            sc.name = request.form.get('name', sc.name).strip()
            sc.filing_status = request.form.get('filing_status', sc.filing_status)
            sc.life_expectancy_age = int(request.form.get('life_expectancy_age', sc.life_expectancy_age))
            sc.growth_rate_mean = float(request.form.get('growth_rate_mean', sc.growth_rate_mean * 100)) / 100
            sc.growth_rate_stddev = float(request.form.get('growth_rate_stddev', sc.growth_rate_stddev * 100)) / 100
            sc.inflation_rate = float(request.form.get('inflation_rate', sc.inflation_rate * 100)) / 100
            sc.notes = request.form.get('notes', '').strip() or None

            # Update people
            for p in sc.people:
                prefix = f'person_{p.id}_'
                p.label = request.form.get(f'{prefix}label', p.label).strip()
                dob_str = request.form.get(f'{prefix}dob')
                if dob_str:
                    p.date_of_birth = datetime.strptime(dob_str, '%Y-%m-%d').date()
                p.retirement_age = int(request.form.get(f'{prefix}retire_age', p.retirement_age))
                p.ss_monthly_benefit = float(request.form.get(f'{prefix}ss', p.ss_monthly_benefit))
                p.ss_start_age = int(request.form.get(f'{prefix}ss_age', p.ss_start_age))

            db.session.commit()
            flash("Scenario updated.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {e}", "danger")

    categories = Category.query.order_by(Category.name).all()
    return render_template('retirement/scenario_form.html', scenario=sc, categories=categories)


# ── Delete Scenario ────────────────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>/delete', methods=['POST'])
def delete_scenario(scenario_id):
    sc = db.session.get(RetirementScenario, scenario_id)
    if sc:
        name = sc.name
        db.session.delete(sc)
        db.session.commit()
        flash(f"Deleted scenario '{name}'.", "success")
    return redirect(url_for('retirement.index'))


# ── Add Person ─────────────────────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>/person/add', methods=['POST'])
def add_person(scenario_id):
    sc = db.session.get(RetirementScenario, scenario_id)
    if sc and len(sc.people) < 2:
        p = RetirementPerson(
            scenario_id=sc.id,
            label=request.form.get('label', 'Spouse').strip(),
            date_of_birth=datetime.strptime(request.form['dob'], '%Y-%m-%d').date(),
            retirement_age=int(request.form.get('retire_age', 65)),
            ss_monthly_benefit=float(request.form.get('ss', 0)),
            ss_start_age=int(request.form.get('ss_age', 67)),
        )
        db.session.add(p)
        db.session.commit()
        flash(f"Added person '{p.label}'.", "success")
    return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))


# ── Account CRUD ───────────────────────────────────────────────────────

@retirement_bp.route('/person/<int:person_id>/account/add', methods=['POST'])
def add_account(person_id):
    p = db.session.get(RetirementPerson, person_id)
    if p:
        acct = RetirementAccount(
            person_id=p.id,
            name=request.form.get('name', 'New Account').strip(),
            tax_type=request.form.get('tax_type', 'traditional'),
            balance=float(request.form.get('balance', 0)),
            monthly_contribution=float(request.form.get('monthly_contribution', 0)),
            growth_override=float(request.form['growth_override']) / 100 if request.form.get('growth_override') else None,
        )
        db.session.add(acct)
        db.session.commit()
        flash(f"Added account '{acct.name}'.", "success")
        return redirect(url_for('retirement.edit_scenario', scenario_id=p.scenario_id))
    return redirect(url_for('retirement.index'))


@retirement_bp.route('/account/<int:account_id>/update', methods=['POST'])
def update_account(account_id):
    acct = db.session.get(RetirementAccount, account_id)
    if acct:
        acct.name = request.form.get('name', acct.name).strip()
        acct.tax_type = request.form.get('tax_type', acct.tax_type)
        acct.balance = float(request.form.get('balance', acct.balance))
        acct.monthly_contribution = float(request.form.get('monthly_contribution', acct.monthly_contribution))
        acct.growth_override = float(request.form['growth_override']) / 100 if request.form.get('growth_override') else None
        db.session.commit()
        flash(f"Updated '{acct.name}'.", "success")
        return redirect(url_for('retirement.edit_scenario', scenario_id=acct.person.scenario_id))
    return redirect(url_for('retirement.index'))


@retirement_bp.route('/account/<int:account_id>/delete', methods=['POST'])
def delete_account(account_id):
    acct = db.session.get(RetirementAccount, account_id)
    if acct:
        scenario_id = acct.person.scenario_id
        db.session.delete(acct)
        db.session.commit()
        flash("Account removed.", "success")
        return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))
    return redirect(url_for('retirement.index'))


# ── Income Source CRUD ─────────────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>/income/add', methods=['POST'])
def add_income(scenario_id):
    src = RetirementIncomeSource(
        scenario_id=scenario_id,
        name=request.form.get('name', 'Income Source').strip(),
        source_type=request.form.get('source_type', 'other'),
        annual_amount=float(request.form.get('annual_amount', 0)),
        start_age=int(request.form.get('start_age', 65)),
        inflation_adjusted=request.form.get('inflation_adjusted') == 'on',
    )
    db.session.add(src)
    db.session.commit()
    flash(f"Added income source '{src.name}'.", "success")
    return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))


@retirement_bp.route('/income/<int:income_id>/delete', methods=['POST'])
def delete_income(income_id):
    src = db.session.get(RetirementIncomeSource, income_id)
    if src:
        scenario_id = src.scenario_id
        db.session.delete(src)
        db.session.commit()
        flash("Income source removed.", "success")
        return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))
    return redirect(url_for('retirement.index'))


# ── Expense Item CRUD ──────────────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>/expense/add', methods=['POST'])
def add_expense(scenario_id):
    item = RetirementExpenseItem(
        scenario_id=scenario_id,
        label=request.form.get('label', 'Expense').strip(),
        category_id=int(request.form['category_id']) if request.form.get('category_id') else None,
        monthly_amount=float(request.form.get('monthly_amount', 0)),
        inflation_adjusted=request.form.get('inflation_adjusted') == 'on',
    )
    db.session.add(item)
    db.session.commit()
    flash(f"Added expense '{item.label}'.", "success")
    return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))


@retirement_bp.route('/expense/<int:expense_id>/delete', methods=['POST'])
def delete_expense(expense_id):
    item = db.session.get(RetirementExpenseItem, expense_id)
    if item:
        scenario_id = item.scenario_id
        db.session.delete(item)
        db.session.commit()
        flash("Expense removed.", "success")
        return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))
    return redirect(url_for('retirement.index'))


@retirement_bp.route('/scenario/<int:scenario_id>/expense/import', methods=['POST'])
def import_expenses(scenario_id):
    """Import expense averages from transaction history."""
    sc = db.session.get(RetirementScenario, scenario_id)
    if not sc:
        return redirect(url_for('retirement.index'))

    averages = get_expense_averages_by_category(months=12)
    added = 0
    for avg in averages:
        item = RetirementExpenseItem(
            scenario_id=sc.id,
            label=avg['category_name'],
            category_id=avg['category_id'],
            monthly_amount=avg['monthly_avg'],
            inflation_adjusted=True,
        )
        db.session.add(item)
        added += 1

    db.session.commit()
    flash(f"Imported {added} expense categories from last 12 months.", "success")
    return redirect(url_for('retirement.edit_scenario', scenario_id=scenario_id))


# ── Portfolio Snapshots ────────────────────────────────────────────────

@retirement_bp.route('/snapshots', methods=['GET', 'POST'])
def snapshots():
    if request.method == 'POST':
        snap = PortfolioSnapshot(
            date=date.fromisoformat(request.form.get('date', str(date.today()))),
            account_label=request.form.get('account_label', '').strip(),
            balance=float(request.form.get('balance', 0)),
        )
        db.session.add(snap)
        db.session.commit()
        flash(f"Snapshot recorded for '{snap.account_label}'.", "success")
        return redirect(url_for('retirement.snapshots'))

    all_snapshots = PortfolioSnapshot.query.order_by(PortfolioSnapshot.date.desc()).all()
    return render_template('retirement/snapshots.html', snapshots=all_snapshots)


@retirement_bp.route('/snapshot/<int:snap_id>/delete', methods=['POST'])
def delete_snapshot(snap_id):
    snap = db.session.get(PortfolioSnapshot, snap_id)
    if snap:
        db.session.delete(snap)
        db.session.commit()
        flash("Snapshot deleted.", "success")
    return redirect(url_for('retirement.snapshots'))


# ── Run Simulation (JSON API) ─────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>/simulate', methods=['POST'])
def simulate(scenario_id):
    sc = RetirementScenario.query.options(
        joinedload(RetirementScenario.people).joinedload(RetirementPerson.accounts),
        joinedload(RetirementScenario.income_sources),
        joinedload(RetirementScenario.expense_items),
    ).get_or_404(scenario_id)

    n_sims = int(request.form.get('n_simulations', 1000))
    service = RetirementService(sc)
    result = service.run_simulation(n_simulations=n_sims)
    accumulation = service.get_accumulation_projection()

    # Load snapshots for overlay
    snaps = PortfolioSnapshot.query.order_by(PortfolioSnapshot.date).all()
    snapshot_data = {}
    for s in snaps:
        snapshot_data.setdefault(s.account_label, []).append({
            'date': s.date.isoformat(),
            'balance': float(s.balance),
        })

    return jsonify({
        'simulation': result,
        'accumulation': accumulation,
        'snapshots': snapshot_data,
    })


# ── Results Page ───────────────────────────────────────────────────────

@retirement_bp.route('/scenario/<int:scenario_id>/results')
def results(scenario_id):
    sc = RetirementScenario.query.options(
        joinedload(RetirementScenario.people).joinedload(RetirementPerson.accounts),
        joinedload(RetirementScenario.income_sources),
        joinedload(RetirementScenario.expense_items),
    ).get_or_404(scenario_id)

    return render_template('retirement/results.html', scenario=sc)
