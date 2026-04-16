"""
Retirement planning engine: tax calculations, accumulation projections,
and Monte Carlo simulation for portfolio longevity.
"""
import copy
import numpy as np
from sqlalchemy import func, extract

from models import db, Transaction, Category

# ── 2025 Federal Tax Brackets ──────────────────────────────────────────

BRACKETS_2025 = {
    'mfj': [
        (23_850, 0.10),
        (96_950, 0.12),
        (206_700, 0.22),
        (394_600, 0.24),
        (501_050, 0.32),
        (751_600, 0.35),
        (float('inf'), 0.37),
    ],
    'single': [
        (11_925, 0.10),
        (48_475, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_525, 0.32),
        (626_350, 0.35),
        (float('inf'), 0.37),
    ],
}

STANDARD_DEDUCTION_2025 = {'mfj': 30_000, 'single': 15_000}
EXTRA_DEDUCTION_65 = {'mfj': 1_600, 'single': 2_000}

LTCG_BRACKETS_2025 = {
    'mfj': [
        (96_700, 0.00),
        (600_050, 0.15),
        (float('inf'), 0.20),
    ],
    'single': [
        (48_350, 0.00),
        (533_400, 0.15),
        (float('inf'), 0.20),
    ],
}

# SS taxation thresholds (combined income = AGI + nontaxable interest + 50% SS)
SS_THRESHOLDS = {
    'mfj': [(32_000, 0.0), (44_000, 0.50), (float('inf'), 0.85)],
    'single': [(25_000, 0.0), (34_000, 0.50), (float('inf'), 0.85)],
}


# ── Tax Calculation Functions ──────────────────────────────────────────

def calc_ss_taxable_fraction(combined_income, filing_status):
    """Return the fraction of SS benefits that is taxable (0, 0.50, or 0.85)."""
    thresholds = SS_THRESHOLDS.get(filing_status, SS_THRESHOLDS['mfj'])
    for limit, fraction in thresholds:
        if combined_income <= limit:
            return fraction
    return 0.85


def calc_ordinary_income_tax(taxable_income, filing_status):
    """Progressive federal tax on ordinary income."""
    brackets = BRACKETS_2025.get(filing_status, BRACKETS_2025['mfj'])
    tax = 0.0
    prev_limit = 0
    for limit, rate in brackets:
        band = min(taxable_income, limit) - prev_limit
        if band > 0:
            tax += band * rate
        prev_limit = limit
        if taxable_income <= limit:
            break
    return tax


def calc_ltcg_tax(ltcg_income, taxable_ordinary, filing_status):
    """Tax on qualified dividends / long-term capital gains."""
    brackets = LTCG_BRACKETS_2025.get(filing_status, LTCG_BRACKETS_2025['mfj'])
    tax = 0.0
    base = taxable_ordinary
    remaining = ltcg_income
    for limit, rate in brackets:
        room = max(0, limit - base)
        applied = min(remaining, room)
        if applied > 0:
            tax += applied * rate
            remaining -= applied
            base += applied
        if remaining <= 0:
            break
    return tax


def calc_annual_tax(ordinary_income, ltcg_income, ss_annual, filing_status, ages):
    """
    Full federal tax for one year.
    ordinary_income: 401k withdrawals + rental + other ordinary
    ltcg_income:     brokerage dividends / capital gains
    ss_annual:       total Social Security benefits received
    ages:            list of ages for people in the household (for 65+ deduction)
    Returns: (total_tax, effective_rate, taxable_ss)
    """
    std_ded = STANDARD_DEDUCTION_2025.get(filing_status, 30_000)
    extra = EXTRA_DEDUCTION_65.get(filing_status, 1_600)
    for age in ages:
        if age >= 65:
            std_ded += extra

    # Determine how much SS is taxable
    combined_income = ordinary_income + ltcg_income + (ss_annual * 0.5)
    ss_fraction = calc_ss_taxable_fraction(combined_income, filing_status)
    taxable_ss = ss_annual * ss_fraction

    agi = ordinary_income + taxable_ss + ltcg_income
    taxable_ordinary = max(0, ordinary_income + taxable_ss - std_ded)
    taxable_ltcg = ltcg_income

    ordinary_tax = calc_ordinary_income_tax(taxable_ordinary, filing_status)
    ltcg_tax = calc_ltcg_tax(taxable_ltcg, taxable_ordinary, filing_status)
    total_tax = ordinary_tax + ltcg_tax

    gross = ordinary_income + ltcg_income + ss_annual
    effective_rate = (total_tax / gross) if gross > 0 else 0.0

    return total_tax, effective_rate, taxable_ss


# ── Withdrawal Strategy ───────────────────────────────────────────────

# Simplified RMD divisor at age 75 (IRS Uniform Lifetime Table ~25.6 yrs)
RMD_DIVISOR = 25.6


def _take_from(btype, buckets, withdrawn, remaining, limit=None):
    """Pull up to `limit` (or all of remaining) from a single bucket."""
    available = max(0.0, buckets.get(btype, 0.0))
    take = min(remaining, available) if limit is None else min(remaining, available, limit)
    if take > 0:
        buckets[btype] = buckets[btype] - take
        withdrawn[btype] = withdrawn.get(btype, 0.0) + take
        remaining -= take
    return remaining


def withdraw_from_buckets(shortfall, buckets, ages=None, pre_trad_ordinary=0.0,
                          filing_status='mfj'):
    """
    Age-phase-aware, tax-bracket-filling withdrawal strategy.

    Phase 1 — Bridge (youngest < 59): VUL → brokerage → Roth → traditional → cash
      VUL policy loans are penalty-free and tax-free; avoid traditional IRA (10% penalty).

    Phase 2 — Early retirement (ages 59–74): bracket-fill then brokerage-first
      Pull just enough from traditional to fill the 10–12% bracket, then draw
      from brokerage (capital gains rates), VUL, Roth, and remaining traditional.

    Phase 3 — Late retirement (75+): force RMD then lean on Roth / VUL
      Force the required minimum distribution from traditional first, then
      supplement with Roth → VUL → brokerage → traditional → cash.
    """
    withdrawn = {}
    remaining = shortfall
    youngest = min(ages) if ages else 65

    if youngest < 59:
        # ── Phase 1: Bridge (pre-59½) ──
        for bt in ['vul', 'brokerage', 'roth', 'traditional', 'cash']:
            remaining = _take_from(bt, buckets, withdrawn, remaining)

    elif youngest < 75:
        # ── Phase 2: Early Retirement — bracket filling ──
        # Headroom: how much more ordinary income fits inside the 12% bracket
        brackets = BRACKETS_2025.get(filing_status, BRACKETS_2025['mfj'])
        std_ded = STANDARD_DEDUCTION_2025.get(filing_status, 30_000)
        # Extra deduction for each person 65+
        extra = EXTRA_DEDUCTION_65.get(filing_status, 1_600)
        n_over_65 = sum(1 for a in (ages or []) if a >= 65)
        effective_std_ded = std_ded + extra * n_over_65
        bracket_12_top = brackets[1][0]  # e.g. $96,950 MFJ
        gross_12_top = bracket_12_top + effective_std_ded
        headroom = max(0.0, gross_12_top - pre_trad_ordinary)

        # Pull traditional IRA up to the bracket headroom first
        remaining = _take_from('traditional', buckets, withdrawn, remaining, limit=headroom)
        # Then: brokerage (cap gains) → VUL (tax-free) → Roth (tax-free) → remaining traditional → cash
        for bt in ['brokerage', 'vul', 'roth', 'traditional', 'cash']:
            remaining = _take_from(bt, buckets, withdrawn, remaining)

    else:
        # ── Phase 3: Late Retirement (75+) — force RMD then lean on Roth/VUL ──
        trad_balance = max(0.0, buckets.get('traditional', 0.0))
        rmd = trad_balance / RMD_DIVISOR
        remaining = _take_from('traditional', buckets, withdrawn, remaining, limit=rmd)
        # Supplement with Roth → VUL → brokerage → remaining traditional → cash
        for bt in ['roth', 'vul', 'brokerage', 'traditional', 'cash']:
            remaining = _take_from(bt, buckets, withdrawn, remaining)

    return withdrawn


# ── Monte Carlo Engine ─────────────────────────────────────────────────

class RetirementService:
    """Runs accumulation projection and Monte Carlo retirement simulation."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.rng = np.random.default_rng()

    def run_simulation(self, n_simulations=1000):
        """
        Run the full two-phase Monte Carlo simulation.
        Returns dict with all data needed for charts.
        """
        sc = self.scenario
        people = list(sc.people)
        if not people:
            return self._empty_result()

        # Determine time range
        earliest_retire = min(p.retirement_age for p in people)
        latest_retire = max(p.retirement_age for p in people)
        youngest_age = min(p.current_age for p in people)
        years_to_retire = earliest_retire - youngest_age
        years_in_retirement = sc.life_expectancy_age - earliest_retire

        if years_to_retire < 0 or years_in_retirement <= 0:
            return self._empty_result()

        total_years = years_to_retire + years_in_retirement

        # Pre-compute annual expenses and income sources
        annual_expenses = float(sum(e.monthly_amount for e in sc.expense_items)) * 12
        income_sources = list(sc.income_sources)

        # Pre-build account data per person for fast simulation
        people_data = []
        for p in people:
            accts = []
            for a in p.accounts:
                accts.append({
                    'balance': float(a.balance),
                    'monthly_contribution': float(a.monthly_contribution),
                    'tax_type': a.tax_type,
                    'growth_override': a.growth_override,
                })
            people_data.append({
                'label': p.label,
                'current_age': p.current_age,
                'retirement_age': p.retirement_age,
                'ss_monthly_benefit': float(p.ss_monthly_benefit),
                'ss_start_age': p.ss_start_age,
                'accounts': accts,
            })

        mean = sc.growth_rate_mean
        stddev = sc.growth_rate_stddev
        inflation = sc.inflation_rate
        filing = sc.filing_status

        # Storage for all simulations
        all_balances = np.zeros((n_simulations, total_years + 1))
        all_success = np.zeros(n_simulations, dtype=bool)

        # Per-year tax/income tracking (use median simulation for display)
        # We'll also track detailed data for the median run
        median_tax_data = None

        for sim in range(n_simulations):
            # Deep copy account balances for this simulation
            sim_people = copy.deepcopy(people_data)

            year_balances = []
            tax_yearly = []

            # Initial total balance
            total_bal = sum(a['balance'] for p in sim_people for a in p['accounts'])
            year_balances.append(total_bal)

            # === ACCUMULATION PHASE ===
            for yr_offset in range(years_to_retire):
                for p in sim_people:
                    p_age = p['current_age'] + yr_offset + 1
                    for acct in p['accounts']:
                        rate = acct['growth_override'] if acct['growth_override'] is not None else mean
                        if acct['tax_type'] == 'cash':
                            r = 0.0
                        else:
                            r = self.rng.normal(rate, stddev)
                        acct['balance'] = acct['balance'] * (1 + r) + acct['monthly_contribution'] * 12

                total_bal = sum(a['balance'] for p in sim_people for a in p['accounts'])
                year_balances.append(max(0, total_bal))

            # === DISTRIBUTION PHASE ===
            # Consolidate into tax-type buckets
            buckets = {}
            for p in sim_people:
                for acct in p['accounts']:
                    bt = acct['tax_type']
                    buckets[bt] = buckets.get(bt, 0.0) + acct['balance']

            failed = False
            for yr_offset in range(years_in_retirement):
                year_num = years_to_retire + yr_offset
                infl_factor = (1 + inflation) ** (year_num)

                # Grow each bucket
                for bt in list(buckets.keys()):
                    if bt == 'cash':
                        continue
                    rate_for_bt = mean
                    r = self.rng.normal(rate_for_bt, stddev)
                    buckets[bt] = buckets[bt] * (1 + r)

                # Calculate income
                ss_total = 0.0
                ages = []
                for p in sim_people:
                    p_age = p['current_age'] + year_num + 1
                    ages.append(p_age)
                    if p_age >= p['ss_start_age']:
                        ss_total += p['ss_monthly_benefit'] * 12 * infl_factor

                other_ordinary = 0.0
                other_ltcg = 0.0
                for src in income_sources:
                    oldest_age = max(p['current_age'] + year_num + 1 for p in sim_people)
                    if oldest_age >= src.start_age:
                        amt = float(src.annual_amount)
                        if src.inflation_adjusted:
                            amt *= infl_factor
                        if src.source_type == 'brokerage_div':
                            other_ltcg += amt
                        else:
                            other_ordinary += amt

                total_income = ss_total + other_ordinary + other_ltcg

                # Calculate expenses
                expenses = annual_expenses * infl_factor

                # Calculate withdrawal needed
                shortfall = max(0, expenses - total_income)

                # Estimate pre-traditional-withdrawal ordinary income for bracket-filling.
                # Use 85% SS taxability as conservative upper bound to avoid over-filling.
                pre_trad_ordinary = other_ordinary + ss_total * 0.85

                # Withdraw from buckets using age-phase strategy
                withdrawn = withdraw_from_buckets(
                    shortfall, buckets, ages=ages,
                    pre_trad_ordinary=pre_trad_ordinary, filing_status=filing,
                )

                # Calculate tax on withdrawn amounts
                ordinary_withdrawn = withdrawn.get('traditional', 0.0)
                ltcg_withdrawn = withdrawn.get('brokerage', 0.0)
                # VUL and Roth withdrawals are tax-free

                total_ordinary = other_ordinary + ordinary_withdrawn
                total_ltcg = other_ltcg + ltcg_withdrawn

                tax, eff_rate, taxable_ss = calc_annual_tax(
                    total_ordinary, total_ltcg, ss_total, filing, ages
                )

                # Withdraw tax from buckets (same phase-aware strategy)
                if tax > 0:
                    withdraw_from_buckets(
                        tax, buckets, ages=ages,
                        pre_trad_ordinary=pre_trad_ordinary, filing_status=filing,
                    )

                tax_yearly.append({
                    'year': year_num,
                    'tax': tax,
                    'effective_rate': eff_rate,
                    'ss_income': ss_total,
                    'ordinary_income': total_ordinary,
                    'ltcg_income': total_ltcg,
                    'expenses': expenses,
                    'total_income': total_income,
                    'withdrawal': shortfall,
                })

                total_bal = sum(max(0, v) for v in buckets.values())
                year_balances.append(total_bal)

                if total_bal <= 0:
                    # Fill remaining years with 0
                    remaining_years = years_in_retirement - yr_offset - 1
                    year_balances.extend([0.0] * remaining_years)
                    failed = True
                    break

            all_balances[sim, :len(year_balances)] = year_balances[:total_years + 1]
            all_success[sim] = not failed

            # Save tax data from the median-ish run (simulation 0 as reference)
            if sim == 0:
                median_tax_data = tax_yearly

        # Compute percentiles
        percentiles = {
            'p10': np.percentile(all_balances, 10, axis=0).tolist(),
            'p25': np.percentile(all_balances, 25, axis=0).tolist(),
            'p50': np.percentile(all_balances, 50, axis=0).tolist(),
            'p75': np.percentile(all_balances, 75, axis=0).tolist(),
            'p90': np.percentile(all_balances, 90, axis=0).tolist(),
        }

        success_rate = float(np.mean(all_success)) * 100

        # Build year labels
        year_labels = []
        for i in range(total_years + 1):
            age = youngest_age + i
            year_labels.append(f"Age {age}")

        return {
            'success_rate': round(success_rate, 1),
            'n_simulations': n_simulations,
            'percentiles': percentiles,
            'year_labels': year_labels,
            'years_to_retire': years_to_retire,
            'total_years': total_years,
            'youngest_age': youngest_age,
            'earliest_retire': earliest_retire,
            'life_expectancy': sc.life_expectancy_age,
            'median_tax_data': median_tax_data or [],
            'median_final_balance': float(np.median(all_balances[:, -1])),
        }

    def get_accumulation_projection(self):
        """
        Deterministic accumulation projection (median path) for the
        'are you on track?' chart. Returns year-by-year projected balances.
        """
        sc = self.scenario
        people = list(sc.people)
        if not people:
            return {'years': [], 'projected': [], 'by_type': {}}

        youngest_age = min(p.current_age for p in people)
        earliest_retire = min(p.retirement_age for p in people)
        years = earliest_retire - youngest_age

        if years <= 0:
            return {'years': [], 'projected': [], 'by_type': {}}

        # Build account list
        accts = []
        for p in people:
            for a in p.accounts:
                accts.append({
                    'balance': float(a.balance),
                    'annual_contrib': float(a.monthly_contribution) * 12,
                    'tax_type': a.tax_type,
                    'growth_rate': a.growth_override if a.growth_override is not None else sc.growth_rate_mean,
                })

        year_labels = []
        projected = []
        by_type = {t: [] for t in ['traditional', 'roth', 'brokerage', 'vul', 'cash']}

        for yr in range(years + 1):
            age = youngest_age + yr
            year_labels.append(f"Age {age}")

            type_totals = {}
            total = 0.0
            for a in accts:
                type_totals[a['tax_type']] = type_totals.get(a['tax_type'], 0.0) + a['balance']
                total += a['balance']

            projected.append(total)
            for t in by_type:
                by_type[t].append(type_totals.get(t, 0.0))

            # Grow for next year (don't grow on last iteration)
            if yr < years:
                for a in accts:
                    if a['tax_type'] == 'cash':
                        a['balance'] += a['annual_contrib']
                    else:
                        a['balance'] = a['balance'] * (1 + a['growth_rate']) + a['annual_contrib']

        return {
            'years': year_labels,
            'projected': projected,
            'by_type': by_type,
        }

    def _empty_result(self):
        return {
            'success_rate': 0,
            'n_simulations': 0,
            'percentiles': {k: [] for k in ['p10', 'p25', 'p50', 'p75', 'p90']},
            'year_labels': [],
            'years_to_retire': 0,
            'total_years': 0,
            'youngest_age': 0,
            'earliest_retire': 0,
            'life_expectancy': 0,
            'median_tax_data': [],
            'median_final_balance': 0,
        }


# ── Expense Pre-Fill from Transaction History ──────────────────────────

SAVINGS_CATS = {'Savings Transfer', 'Investment Transfer', 'Investment', 'VUL'}
EXCLUDED_CAT = 'Ignored Credit Card Payment'


def get_expense_averages_by_category(months=12):
    """
    Query the last N months of transactions and return average monthly
    spend per expense category. Used for expense pre-fill.
    Returns list of dicts: [{'category_id': int, 'category_name': str, 'monthly_avg': float}]
    """
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=months * 30)

    rows = db.session.query(
        Category.id,
        Category.name,
        func.sum(Transaction.amount),
        func.count(func.distinct(extract('month', Transaction.date) + extract('year', Transaction.date) * 100))
    ).join(Transaction.category).filter(
        Transaction.date >= cutoff,
        Transaction.is_deleted == False,
        Transaction.amount < 0,
        Category.name != EXCLUDED_CAT,
        ~Category.name.in_(SAVINGS_CATS),
        Category.type == 'Expense',
    ).group_by(Category.id, Category.name).all()

    results = []
    for cat_id, cat_name, total_spent, month_count in rows:
        if month_count and month_count > 0:
            monthly_avg = abs(float(total_spent)) / month_count
        else:
            monthly_avg = 0
        if monthly_avg > 0:
            results.append({
                'category_id': cat_id,
                'category_name': cat_name,
                'monthly_avg': round(monthly_avg, 2),
            })

    results.sort(key=lambda x: x['monthly_avg'], reverse=True)
    return results
