# Refactoring Plan - Money Tracker

## Completed ✅
- Created `models.py` with all database models
- Created `utils/pdf_parsers.py` with PDF parsing functions
- Created `utils/helpers.py` with helper functions and constants
- Created `services/dashboard.py` with DashboardService class
- Removed old `Budget` model from models.py
- Created backup of original app.py

## Deprecated Routes to Remove ❌
These are old budget system routes that need deletion:
1. `/monthly_averages` (lines 1552-1579)
2. `/api/average_data` (lines 1581-1687)
3. `/api/save_budget` (lines 1711-1742)
4. `/api/load_budget` (lines 1744-1753)
5. `/api/delete_budget` (lines 1755-1759)

## Routes to Keep ✅
Modern budget system (BudgetPlan):
- `/budget` - Budget page with plans
- `/api/budget_plan` - CRUD for budget plans
- `/api/budget_plan/<id>/activate`
- `/api/budget_plan/<id>/delete`
- `/api/budget_plan/<id>/items`
- `/api/budget_item` - CRUD for budget line items
- `/api/budget_vs_actual` - Budget vs actual comparison

## Template to Delete
- `templates/monthly_averages.html`

## Next Steps
1. Remove references to old `Budget` model in app.py (if any remain)
2. Delete the 5 deprecated routes listed above
3. Delete monthly_averages.html template
4. Test the application runs without errors
