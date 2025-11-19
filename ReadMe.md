# Personal Finance Dashboard

A robust, locally-hosted personal finance application built with **Flask**, **PostgreSQL**, **SQLAlchemy**, and **Plotly**. This dashboard is designed to track not just "spending," but total **liquidity, cash flow, and wealth retention** across checking, savings, and investment accounts.

## 🚀 Features

* **Multi-Source Import:** Parse **Chase PDF Statements** and generic CSVs automatically.
* **Smart Categorization:** Regex-based rules engine with "Bulk Force Apply" to retroactively fix history.
* **Double-Entry Logic:** Handles internal transfers (Checking ↔ Savings) without double-counting them as expenses.
* **Strategic Exclusion:** Automatically ignores Credit Card Payments to prevent "double-grossing" outflows.
* **Interactive Charts:** Plotly-based visualizations with time-travel sliders and zoom capabilities.
* **AI Insights:** (Optional) Integration with Google Gemini to analyze monthly trends.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
* Python 3.10+
* PostgreSQL (Local or Remote)

### 2. Environment Variables
Create a `.env` file in the root directory:

```ini
# Database Connection (PostgreSQL)
DATABASE_URL=postgresql://username:password@localhost:5432/budget_db

# Optional: For AI Summaries (Leave blank if not using)
GEMINI_API_KEY=your_google_api_key_here
````

### 3\. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4\. Initialize Database

The application automatically checks for tables on startup. If they don't exist, it creates them and seeds default categories.

1.  Create the database in Postgres: `CREATE DATABASE budget_db;`
2.  Run the app once to generate tables.

### 5\. Run the Application

```bash
python app.py
```

Access the dashboard at `http://127.0.0.1:5000/`.

-----

## 🧠 The "Ecosystem" Logic (Crucial)

This app treats your finances as a **Walled Garden**. It distinguishes between "New Wealth" entering the garden, "Lost Wealth" leaving it, and "Moving Money" inside it.

### Category Types

1.  **Income:** Money entering from outside (Salary, Dividends, Venmo).
2.  **Expense:** Money leaving forever (Groceries, Gas, Bills).
3.  **Transfer:** Money moving between accounts (Checking ↔ Savings) or to/from tracked assets (Fidelity). **Transfers are generally excluded from "Spending" charts to preserve accuracy.**

### The "Credit Card Payment" Rule

The app has a hard-coded logic to **ignore** transactions named exactly `Transfer Credit Card Payment` in almost all charts.

  * **Reason:** You "spent" the money when you bought the item (e.g., $50 Gas). If we counted the $50 CC bill payment as *another* expense, your dashboard would show $100 outflow. Ignoring the payment fixes this "Double Grossing" error.

-----

## 📊 Dashboard Charts Explained

### 1\. Total Net Cash Flow (Sustainability)

**"Did my total available cash grow or shrink this month?"**

  * **Green Bar (Inflow):** Sum of all positive transactions (Income + Transfers In).
      * *Includes:* Salary, plus money pulled from Fidelity/Investment accounts.
  * **Red Bar (Outflow):** Sum of all negative transactions (Expenses + Transfers Out).
      * *Includes:* Living costs, plus money sent to external accounts (e.g., Money Market).
  * **Blue Line:** The net result. If flat ($0), you are liquid.
  * *Note:* Internal transfers (Checking ↔ Savings) cancel out here, resulting in a neutral net change.

### 2\. Monthly Net Savings (Growth)

**"Did I add to my savings pile or dip into it?"**

  * **Scope:** Looks **ONLY** at accounts marked as `Savings`.
  * **Green Bar:** Deposits into savings (Transfers from Checking).
  * **Red Bar:** Withdrawals from savings (Transfers back to Checking).
  * **Logic:** Direct expenses paid from Savings (like a car note) are **IGNORED**. This chart strictly measures your *intent to save*, not your bill payments.

### 3\. Core Operating Performance

**"Does my regular job cover my regular life?"**

  * **Core Income:** Strictly Type='Income' (Salary, Rental). **Excludes** transfers from investments.
  * **Core Expenses:** Daily living costs. **Excludes** specific large/strategic categories: `Car Payment` and `Insurance`.
  * **Purpose:** To see if your day-to-day lifestyle is sustainable without relying on investment withdrawals or one-time large bills.

### 4\. Income vs. Expenses (Liquidity View)

**"Was I able to fund my lifestyle this month?"**

  * **Income (Green):** Salary + **Transfers from Fidelity/Investments**.
      * *Why?* If you sell stock to pay for a car, that cash acts like income for that month. This chart treats it as "Realized Liquidity."
  * **Expense (Red):** Total spending on goods/services.

-----

## 📝 Categorization Guide (Examples)

To make the charts work correctly, use these categorization strategies:

| Scenario | Transaction | Category Type | Category Name (Example) | Result on Dashboard |
| :--- | :--- | :--- | :--- | :--- |
| **Salary** | +$4,000 | **Income** | `Paycheck` | Increases Income & Net Worth. |
| **Paying CC Bill** | -$1,000 | **Transfer** | `Transfer Credit Card Payment` | **IGNORED** (Prevents double counting). |
| **Saving Money** | -$500 (Chk) | **Transfer** | `Transfer to Savings` | Neutral on Expense Chart. Green on Savings Trend. |
| **Using Savings** | +$500 (Chk) | **Transfer** | `Transfer from Savings` | Neutral on Expense Chart. Red on Savings Trend. |
| **Buying Stock** | -$1,000 | **Transfer** | `Transfer Fidelity` | Neutral on Expense Chart (Asset Reallocation). |
| **Selling Stock** | +$1,000 | **Transfer** | `Transfer Fidelity` | Increases "Income" on Liquidity Chart. |
| **Car Payoff** | -$13,000 | **Expense** | `Car Payment` | Shows as Expense. Excluded from "Core Operating." |

-----

## 📂 Project Structure

  * **`app.py`**: Main Flask application. Contains `DashboardService` (chart logic) and all database models.
  * **`templates/`**: HTML files using Tailwind CSS via CDN (or local static files).
  * **`static/`**:
      * `js/tailwind.js`: Local Tailwind script.
      * `css/fontawesome/`: Local FontAwesome icons.
  * **`requirements.txt`**: Python dependencies.

## ⚡ Troubleshooting

**"My charts show No Data / Jan 2000"**

  * This usually means a date formatting issue. The app expects ISO dates (`YYYY-MM-DD`) for the X-axis. If you customized the Python code, ensure `xaxis_config` uses `type='category'` or `type='date'` consistently with the data provided.

**"I categorized a transaction but the chart didn't update."**

  * Charts are cached or aggregated. Ensure you didn't accidentally mark the transaction as "Deleted".
  * If using the "Rules" engine, remember to click the **Green Play Button** (Force Apply) in "Manage Rules" to apply a new rule to *old* transactions.