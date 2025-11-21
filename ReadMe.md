# Personal Finance & Expense Tracker

A Python Flask-based web application designed to track personal expenses, categorize spending, and visualize trends using automated rules and an intuitive dashboard.

## 🚀 Features

### 1. Dashboard
* **Income vs Expenses (Budget View):** Tracks your "earnings" (salary, dividends) vs. "spending" (purchases minus refunds). Transfers between your own accounts are ignored to show true profit/loss.
* **Total Cash Flow (Liquidity View):** Tracks ALL money movement. Borrowing a loan counts as inflow; paying a credit card bill counts as outflow. Perfect for answering "Do I have cash available?".
* **Core Operating Performance:** A focused view of your discretionary spending (Groceries, Dining, Shopping) by filtering out large fixed costs like Rent/Mortgage, Car Payments, and Insurance.
* **Savings Growth:** Visualizes net transfers into savings accounts.
* **Top Payees:** A ranking of where you spend the most money (grouped by payee name).
* **Year-over-Year (YoY) Comparison:** Compares your cumulative net spending this year vs. last year to track lifestyle inflation.

### 2. Transaction Management
* **File Upload:** Supports CSV and PDF uploads.
* **Contra-Expense Logic:** Refunds are treated as negative expenses, not income.
    * *Example:* Spending \$100 and getting a \$20 refund results in an \$80 expense for that category.
* **Duplicate Handling:**
    * **Blind Trust:** The system does NOT check for duplicates. It assumes the user uploads the correct files. If you upload the same file twice, the transactions will be duplicated.
* **CSV Format:**
    * Files must follow a strict 5-column layout (header names are ignored, only position matters):
    * `Column 0` = Date
    * `Column 1` = Amount
    * `Column 2` = (Ignored)
    * `Column 3` = (Ignored)
    * `Column 4` = Description

### 3. Categorization Engine
* **Payee Rules:** Create rules to automatically categorize transactions based on text matches (e.g., "PUBLIX" -> "Groceries").
* **Review Queue:** Any transaction without a rule lands in the "Review Transactions" inbox for manual categorization.
* **Bulk Updates:** Creating a rule applies it historically to all matching past transactions.

### 4. AI Insights (Toggleable)
* Integrated with Google Gemini API.
* Provides text-based analysis of your monthly and yearly spending trends.
* **On-Demand:** Insights are hidden by default and only generate when you toggle the switch on the dashboard.

## 🛠️ Technical Stack
* **Backend:** Python, Flask, SQLAlchemy (SQLite/PostgreSQL)
* **Frontend:** HTML, Tailwind CSS, Plotly.js (Charts)
* **Data Processing:** Pandas, PDFPlumber

## 📦 Setup & Installation

1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Environment Variables:**
    Create a `.env` file with the following:
    ```ini
    DATABASE_URL=postgresql://user:pass@localhost:5432/budget_db
    GEMINI_API_KEY=your_api_key_here
    ```

3.  **Run the App:**
    ```bash
    python app.py
    ```

4.  **Access:**
    Open your browser to `http://localhost:5000`

## 💡 Usage Tips
* **Uploads:** Since duplicate detection is disabled, be careful not to re-upload the same bank statement twice.
* **Refunds:** Always categorize a refund to the same category as the original purchase (e.g., a refund from Home Depot should be categorized as "Home Improvement", not "Income").