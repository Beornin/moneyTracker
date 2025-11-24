import random
from datetime import date, timedelta
import json
from app import app, db, Account, Category, Payee, PayeeRule, Transaction, Event, Budget, StatementRecord

def get_date_range(start_date, end_date):
    """Generator for iterating through dates."""
    for n in range(int((end_date - start_date).days) + 1):
        yield start_date + timedelta(n)

def seed_database():
    print("🚀 Starting Database Seeding...")
    
    with app.app_context():
        # 1. CLEANUP: Wipe all data and recreate tables
        print("   🧹 Clearing old data...")
        db.drop_all()
        db.create_all()

        # 2. ACCOUNTS (No starting_balance)
        print("   🏦 Creating Accounts...")
        acc_checking = Account(name='Wells Fargo Checking', account_type='checking')
        acc_cc = Account(name='Chase Sapphire', account_type='credit_card')
        acc_savings = Account(name='High Yield Savings', account_type='savings')
        acc_hsa = Account(name='HealthEquity HSA', account_type='hsa')
        
        db.session.add_all([acc_checking, acc_cc, acc_savings, acc_hsa])
        db.session.commit()

        # 3. CATEGORIES
        print("   🏷️  Creating Categories...")
        cats = {
            # Income
            'Salary': Category(name='Salary', type='Income'),
            'Bonus': Category(name='Bonus', type='Income'),
            'Interest': Category(name='Interest Income', type='Income'),
            
            # Core Expenses
            'Groceries': Category(name='Groceries', type='Expense'),
            'Eat Out': Category(name='Eat Out', type='Expense'),
            'Housing': Category(name='Housing', type='Expense'),
            'Utilities': Category(name='Utilities', type='Expense'),
            'Transportation': Category(name='Transportation', type='Expense'),
            'Medical': Category(name='Medical', type='Expense'),
            'Entertainment': Category(name='Entertainment', type='Expense'),
            'Shopping': Category(name='Shopping', type='Expense'),
            
            # Strategic/Fixed
            'Car Payment': Category(name='Car Payment', type='Expense'),
            'Insurance': Category(name='Insurance', type='Expense'),
            
            # Transfers
            'Transfer Savings': Category(name='Transfer Checking->Savings', type='Transfer'),
            'Transfer CC Payment': Category(name='Transfer Credit Card Payment', type='Transfer'),
            'Uncategorized': Category(name='Uncategorized', type='Expense')
        }
        db.session.add_all(cats.values())
        db.session.commit()

        # 4. PAYEES & RULES
        print("   👤 Creating Payees & Rules...")
        # Helper to create rule + payee
        def create_payee_flow(name, rule_frag, cat_obj):
            rule = PayeeRule(fragment=rule_frag, display_name=name, category_id=cat_obj.id)
            db.session.add(rule)
            db.session.commit() # Commit to get ID
            payee = Payee(name=f"{name} Store #123", rule_id=rule.id)
            db.session.add(payee)
            return payee

        p_publix = create_payee_flow('Publix', 'PUBLIX', cats['Groceries'])
        p_mcd = create_payee_flow('McDonalds', 'MCDONALDS', cats['Eat Out'])
        p_shell = create_payee_flow('Shell Oil', 'SHELL', cats['Transportation'])
        p_netflix = create_payee_flow('Netflix', 'NETFLIX', cats['Entertainment'])
        p_amazon = create_payee_flow('Amazon', 'AMZN', cats['Shopping'])
        p_employer = create_payee_flow('My Employer', 'PAYROLL', cats['Salary'])
        p_dominion = create_payee_flow('Dominion Energy', 'DOMINION', cats['Utilities'])
        p_toyota = create_payee_flow('Toyota Financial', 'TOYOTA', cats['Car Payment'])
        p_cvs = create_payee_flow('CVS Pharmacy', 'CVS', cats['Medical'])
        p_quest = create_payee_flow('Quest Diagnostics', 'QUEST', cats['Medical'])
        p_cc_payment = create_payee_flow('Chase Payment', 'CHASE CREDIT', cats['Transfer CC Payment'])
        p_transfer_sav = create_payee_flow('Savings Transfer', 'TRANSFER', cats['Transfer Savings'])

        db.session.commit()

        # 5. GENERATE TRANSACTIONS (2 Years History)
        print("   💸 Generating Transactions (This may take a moment)...")
        start_date = date(2024, 1, 1)
        end_date = date(2025, 12, 31)
        transactions = []

        # A. Monthly Income (Salary)
        current = start_date
        while current <= end_date:
            # Payday on 1st and 15th
            if current.day == 1 or current.day == 15:
                transactions.append(Transaction(
                    date=current, original_description='Direct Dep Payroll', amount=2500.00,
                    payee_id=p_employer.id, category_id=cats['Salary'].id, account_id=acc_checking.id
                ))
            
            # Monthly Bills (Rent/Utilities) on 5th
            if current.day == 5:
                transactions.append(Transaction(
                    date=current, original_description='Dominion Power Bill', amount=-150.00,
                    payee_id=p_dominion.id, category_id=cats['Utilities'].id, account_id=acc_checking.id
                ))
            
            # Car Payment on 20th
            if current.day == 20:
                transactions.append(Transaction(
                    date=current, original_description='Toyota Financial Svc', amount=-450.00,
                    payee_id=p_toyota.id, category_id=cats['Car Payment'].id, account_id=acc_checking.id
                ))

            # Savings Transfer on 25th
            if current.day == 25:
                # Out from Checking
                transactions.append(Transaction(
                    date=current, original_description='Transfer to Savings', amount=-500.00,
                    payee_id=p_transfer_sav.id, category_id=cats['Transfer Savings'].id, account_id=acc_checking.id
                ))
                # In to Savings
                transactions.append(Transaction(
                    date=current, original_description='Transfer from Checking', amount=500.00,
                    payee_id=p_transfer_sav.id, category_id=cats['Transfer Savings'].id, account_id=acc_savings.id
                ))

            # Credit Card Payment on 28th
            if current.day == 28:
                payment_amt = 1500.00 # Simplified flat payment
                # Out from Checking
                transactions.append(Transaction(
                    date=current, original_description='Chase Credit Crd Epay', amount=-payment_amt,
                    payee_id=p_cc_payment.id, category_id=cats['Transfer CC Payment'].id, account_id=acc_checking.id
                ))
                # In to Credit Card
                transactions.append(Transaction(
                    date=current, original_description='Payment Thank You', amount=payment_amt,
                    payee_id=p_cc_payment.id, category_id=cats['Transfer CC Payment'].id, account_id=acc_cc.id
                ))
            
            current += timedelta(days=1)

        # B. Variable Daily Spending (Groceries, Dining, Gas)
        # Run through every day
        for day in get_date_range(start_date, end_date):
            # Weekly Grocery Trip (Random day of week)
            if day.weekday() == 5: # Saturday
                amt = -1 * random.uniform(120, 200)
                transactions.append(Transaction(
                    date=day, original_description='Publix #1022', amount=amt,
                    payee_id=p_publix.id, category_id=cats['Groceries'].id, account_id=acc_cc.id
                ))
            
            # Frequent Dining Out (30% chance any day)
            if random.random() < 0.3:
                amt = -1 * random.uniform(15, 45)
                transactions.append(Transaction(
                    date=day, original_description='McDonalds 992', amount=amt,
                    payee_id=p_mcd.id, category_id=cats['Eat Out'].id, account_id=acc_cc.id
                ))

            # Gas every ~10 days
            if day.day in [1, 11, 21]:
                amt = -1 * random.uniform(40, 60)
                transactions.append(Transaction(
                    date=day, original_description='Shell Oil 123', amount=amt,
                    payee_id=p_shell.id, category_id=cats['Transportation'].id, account_id=acc_cc.id
                ))

        # C. HSA Expenses (Medical Only - The new requirement)
        # Random medical bills once a month
        current = start_date
        while current <= end_date:
            if current.day == 14:
                # CVS Prescription
                amt = -1 * random.uniform(15, 50)
                transactions.append(Transaction(
                    date=current, original_description='CVS Pharmacy', amount=amt,
                    payee_id=p_cvs.id, category_id=cats['Medical'].id, account_id=acc_hsa.id
                ))
            
            # Quest Diagnostics (Quarterly)
            if current.day == 2 and current.month % 3 == 0:
                amt = -1 * random.uniform(100, 250)
                transactions.append(Transaction(
                    date=current, original_description='Quest Diagnostics', amount=amt,
                    payee_id=p_quest.id, category_id=cats['Medical'].id, account_id=acc_hsa.id
                ))
            
            current += timedelta(days=1)

        # Bulk insert for performance
        print(f"   💾 Saving {len(transactions)} transactions...")
        db.session.add_all(transactions)
        db.session.commit()

        # 6. SAVED BUDGETS
        print("   💼 Creating Saved Budgets...")
        budget_criteria = json.dumps({
            'category_ids': [str(cats['Groceries'].id), str(cats['Eat Out'].id), str(cats['Utilities'].id)],
            'payee_names': [],
            'account_ids': [str(acc_checking.id), str(acc_cc.id)],
            # Optional dates can be passed here if your model expects them in JSON, 
            # but based on app.py they are likely columns now. 
            # We will set columns below.
        })
        budget = Budget(
            name='Core Living 2024', 
            criteria=budget_criteria, 
            start_date=date(2024, 1, 1), 
            end_date=date(2024, 12, 31)
        )
        db.session.add(budget)
        db.session.commit()

        # 7. EVENTS
        print("   📅 Creating Life Events...")
        events = [
            Event(date=date(2024, 6, 1), description="Bought New Car"),
            Event(date=date(2025, 1, 1), description="New Years Resolution"),
            Event(date=date(2024, 3, 15), description="Bonus Payout")
        ]
        db.session.add_all(events)
        db.session.commit()

    print("✅ Database seeded successfully! You can now run 'python app.py'.")

if __name__ == '__main__':
    seed_database()