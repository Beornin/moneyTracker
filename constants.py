# --- SHARED CONSTANTS ---

# How many months to show on dashboard charts
DASHBOARD_MONTH_SPAN = 12

# Category name excluded from income/expense calculations (CC payment from checking = not real income)
EXCLUDED_CAT = 'Ignored Credit Card Payment'

# For us these categories are from a different pot of money outside this system,
# so do not use them for core calculations
EXCLUDED_CAT_CORE = []

# For us these specific payees are from a different pot of money outside this system,
# so do not use them for core calculations
EXCLUDED_PAYEE_LABELS_CORE = []

# Entity name for the joint Fidelity brokerage link (checking <-> brokerage transfers).
# Withdrawals FROM this entity are flagged red on cash-flow charts; withdrawals from any
# other brokerage-linked entity (e.g. an individual retirement account) are not, since
# pulling from the joint account is the one that matters for household cash flow.
JOINT_FIDELITY_ENTITY_NAME = 'Joint Fidelity'

# Categories representing money moving between our own accounts / into investments
# rather than being consumed. Wealth-building, not spending.
INTERNAL_FLOW_CATS = {'Savings Transfer', 'Investment Transfer', 'Investment', 'VUL'}

# Investment proceeds that land in checking (IRA distributions, brokerage income).
# Real money, but NOT normal recurring household income -- a one-off IRA rollover
# would otherwise read as a huge "income" month and hide whether we can actually
# live off our paychecks. Excluded from the zero-based budget waterfall.
INVESTMENT_INCOME_CATS = {'Empower IRA', 'Investment Income'}

# --- NET WORTH TRACKER ---

NET_WORTH_ASSET_CATEGORIES = [
    'Cash', 'Brokerage/Investment', 'Retirement', 'Education (529)',
    'Bonds', 'Life Insurance Cash Value', 'Real Estate', 'Vehicle', 'Other Asset',
]

NET_WORTH_DEBT_CATEGORIES = [
    'Mortgage', 'Auto Loan', 'Student Loan', 'Credit Card', 'Personal Loan', 'Other Debt',
]

NET_WORTH_LIQUIDITY_HORIZONS = ['now', 'now_mid', 'mid_long', 'long']
