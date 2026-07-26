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

# --- NET WORTH TRACKER ---

NET_WORTH_ASSET_CATEGORIES = [
    'Cash', 'Brokerage/Investment', 'Retirement', 'Education (529)',
    'Bonds', 'Life Insurance Cash Value', 'Real Estate', 'Vehicle', 'Other Asset',
]

NET_WORTH_DEBT_CATEGORIES = [
    'Mortgage', 'Auto Loan', 'Student Loan', 'Credit Card', 'Personal Loan', 'Other Debt',
]

NET_WORTH_LIQUIDITY_HORIZONS = ['now', 'now_mid', 'mid_long', 'long']
