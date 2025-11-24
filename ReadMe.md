# Personal Finance & Expense Tracker

A robust Flask-based personal finance dashboard designed for granular control over expenses, income, and savings. It features automated PDF statement parsing (Chase & HealthEquity HSA), customizable categorization rules, detailed trend analysis, and AI-powered insights.

## 🚀 Features

### 1. Dashboards & Analysis
* **Main Dashboard**: 
    * **Income vs. Expense**: Monthly P&L excluding transfers and hidden categories.
    * **Core Operating**: Tracks "Day-to-Day" lifestyle costs, filtering out large fixed costs (like Mortgages/Car Payments) to show true discretionary spending.
    * **Savings Rate**: Visualizes money moving into vs. out of Savings accounts.
    * **HSA Activity**: A dedicated section for Health Savings Account expenses (medical only), separated from the main operating budget.
* **Trend Analysis**: Interactive line/bar/area charts to visualize spending over time by Category, Payee, or Account.
* **Monthly Averages**: A powerful calculator to determine the average monthly spend for specific categories or payees over a selected date range.
    * **Drill-down**: View sub-rows for individual payees within a category total.
    * **Smart Filtering**: Excludes a payee from the category total if that payee is also selected individually (prevents double counting).
    * **Budget Saving**: Save your specific filter sets (e.g., "Groceries + Dining + Gas") as named Budgets to reload later.

### 2. Data Management
* **Smart Import**:
    * **Chase PDFs**: Automatically parses transactions and statement dates.
    * **HSA PDFs**: Parses HealthEquity statements, strictly filtering for *expenses* (withdrawals) and ignoring contributions.
    * **Duplicate Prevention**: Tracks uploaded statement periods in the database to prevent re-importing the same file.
* **Categorization Engine**:
    * **Payee Rules**: Create "Contains" rules (e.g., "PUBLIX" -> "Groceries") to automatically categorize transactions.
    * **Review Queue**: Transactions without a match land in a queue for manual review.
    * **Bulk Updates**: Creating a rule applies it historically to all matching past transactions.

### 3. AI Insights
* Integrated with **Google Gemini API**.
* Provides on-demand text-based analysis of your monthly and yearly spending trends.
* **Privacy**: Insights are generated only when you click the toggle; data is sent ephemerally to the API.

## 🛠️ Technical Stack
* **Backend**: Python, Flask, SQLAlchemy (PostgreSQL)
* **Frontend**: HTML5, Tailwind CSS, Plotly.js
* **Data Processing**: Pandas, PDFPlumber

## 📦 Setup & Installation

### Prerequisites
* Python 3.10+
* PostgreSQL Database
* Create a .env file in the root directory. You must include the Gemini API key for insights to work!

### 1. Installation
```bash
git clone <repository-url>
cd finance-app
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt