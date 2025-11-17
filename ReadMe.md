Python Budgeting App with Postgres & Flask

This is a personal budgeting application designed to run on your local desktop. It uses a Flask (Python) backend, a PostgreSQL database for storage, and Plotly for interactive charts.

Features

Web Interface: Access your dashboard from a local URL (http://127.0.0.1:5000).

Account Management: Create different account types (Checking, Savings, Credit Card).

CSV Ingestion: Upload transaction CSVs from your bank and assign them to an account.

Transfer Handling: Internal transfers (e.g., Savings to Checking) are filtered out of income/expense reports to prevent inflated totals.

Interactive Dashboard:

Income vs. Expense Over Time (Transfers Excluded)

Expense by Category Pie Chart (Transfers Excluded)

Net Worth Over Time (Transfers Included for accurate balance tracking)

Year-over-Year Monthly Expense Comparison (Transfers Excluded)

Setup Instructions

Step 1: Set up the PostgreSQL Database

Install PostgreSQL: If you don't already have it, download and install PostgreSQL for your operating system.

Create a User (Optional but Recommended):

Open the psql shell or your preferred Postgres management tool.

CREATE USER budget_user WITH PASSWORD 'your_secure_password_here';

Create the Database:

CREATE DATABASE budget_db;

Grant Privileges:

GRANT ALL PRIVILEGES ON DATABASE budget_db TO budget_user;

\q to quit.

Step 2: Set up the Python Environment

Create a Virtual Environment:

Open your terminal in this project's directory.

python -m venv venv

Activate it:

macOS/Linux: source venv/bin/activate

Windows: .\venv\Scripts\activate

Install Dependencies:

pip install -r requirements.txt

Step 3: Configure the Application

Edit app.py:

Open the app.py file.

Find the line starting with app.config['SQLALCHEMY_DATABASE_URI'].

Update the connection string with your Postgres user, password, and database name from Step 1.

Example: postgresql://budget_user:your_secure_password_here@localhost:5432/budget_db

Customize CSV Parsing (CRITICAL!):

Find the /upload route in app.py (around line 240).

Look for the comment block ### START CUSTOM CSV PARSING LOGIC ###.

You MUST adapt the column_map to match the exact headers in the CSV files exported from your bank.

Example of a necessary change if your CSV uses Posted Date instead of Date:

column_map = {
    'Posted Date': 'date', # Changed from 'Date'
    'Payee Name': 'description', # Changed from 'Description'
    'Amount': 'amount' 
}


Step 4: Run the Application

Run the Flask App:

With your virtual environment still active, run:

python app.py

The first time you run this, it will automatically create all the necessary tables in your budget_db database.

Access Your Dashboard:

Open your web browser and go to:

http://127.0.0.1:5000

How to Use:

First, create accounts using the "Add New Account" form.

Second, select an account, choose a CSV file, and click "Upload" to ingest your transaction data. The dashboard charts will update automatically.