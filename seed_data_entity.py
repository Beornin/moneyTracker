import random
from datetime import date, timedelta
import json
from app import app
from models import db, Account, Category, Entity, Transaction, Event, Budget, StatementRecord

def get_date_range(start_date, end_date):
    """Generator for iterating through dates."""
    for n in range(int((end_date - start_date).days) + 1):
        yield start_date + timedelta(n)

def seed_database():
    print("🚀 Starting Database Seeding (Entity Model)...")
    
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

        # 4. ENTITIES (Replaces Payees & PayeeRules)
        print("   🎯 Creating Entities...")
        
        entities = {
            # Groceries
            'Publix': Entity(
                name='Publix',
                category_id=cats['Groceries'].id,
                match_patterns=['PUBLIX', 'PUBLIX SUPER'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Dining
            'McDonalds': Entity(
                name='McDonalds',
                category_id=cats['Eat Out'].id,
                match_patterns=['MCDONALDS', 'MCDONALD'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Transportation
            'Shell Oil': Entity(
                name='Shell Oil',
                category_id=cats['Transportation'].id,
                match_patterns=['SHELL', 'SHELL OIL'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Entertainment
            'Netflix': Entity(
                name='Netflix',
                category_id=cats['Entertainment'].id,
                match_patterns=['NETFLIX'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Shopping
            'Amazon': Entity(
                name='Amazon',
                category_id=cats['Shopping'].id,
                match_patterns=['AMZN', 'AMAZON', 'PRIME'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Income
            'Employer': Entity(
                name='My Employer',
                category_id=cats['Salary'].id,
                match_patterns=['PAYROLL', 'DIRECT DEP'],
                match_type='positive',
                is_auto_created=False
            ),
            
            # Utilities
            'Dominion Energy': Entity(
                name='Dominion Energy',
                category_id=cats['Utilities'].id,
                match_patterns=['DOMINION', 'DOMINION ENERGY'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Car Payment
            'Toyota Financial': Entity(
                name='Toyota Financial',
                category_id=cats['Car Payment'].id,
                match_patterns=['TOYOTA', 'TOYOTA FINANCIAL'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Medical
            'CVS Pharmacy': Entity(
                name='CVS Pharmacy',
                category_id=cats['Medical'].id,
                match_patterns=['CVS', 'CVS PHARMACY'],
                match_type='any',
                is_auto_created=False
            ),
            
            'Quest Diagnostics': Entity(
                name='Quest Diagnostics',
                category_id=cats['Medical'].id,
                match_patterns=['QUEST', 'QUEST DIAGNOSTICS'],
                match_type='any',
                is_auto_created=False
            ),
            
            # Transfers
            'Chase Payment': Entity(
                name='Chase Payment',
                category_id=cats['Transfer CC Payment'].id,
                match_patterns=['CHASE CREDIT', 'CHASE EPAY', 'PAYMENT THANK YOU'],
                match_type='any',
                is_auto_created=False
            ),
            
            'Savings Transfer': Entity(
                name='Savings Transfer',
                category_id=cats['Transfer Savings'].id,
                match_patterns=['TRANSFER TO SAVINGS', 'TRANSFER FROM CHECKING'],
                match_type='any',
                is_auto_created=False
            ),
        }
        
        db.session.add_all(entities.values())
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
                    entity_id=entities['Employer'].id, category_id=cats['Salary'].id, account_id=acc_checking.id
                ))
            
            # Monthly Bills (Utilities) on 5th
            if current.day == 5:
                transactions.append(Transaction(
                    date=current, original_description='Dominion Power Bill', amount=-150.00,
                    entity_id=entities['Dominion Energy'].id, category_id=cats['Utilities'].id, account_id=acc_checking.id
                ))
            
            # Car Payment on 20th
            if current.day == 20:
                transactions.append(Transaction(
                    date=current, original_description='Toyota Financial Svc', amount=-450.00,
                    entity_id=entities['Toyota Financial'].id, category_id=cats['Car Payment'].id, account_id=acc_checking.id
                ))

            # Savings Transfer on 25th
            if current.day == 25:
                # Out from Checking
                transactions.append(Transaction(
                    date=current, original_description='Transfer to Savings', amount=-500.00,
                    entity_id=entities['Savings Transfer'].id, category_id=cats['Transfer Savings'].id, account_id=acc_checking.id
                ))
                # In to Savings
                transactions.append(Transaction(
                    date=current, original_description='Transfer from Checking', amount=500.00,
                    entity_id=entities['Savings Transfer'].id, category_id=cats['Transfer Savings'].id, account_id=acc_savings.id
                ))

            # Credit Card Payment on 28th
            if current.day == 28:
                payment_amt = 1500.00
                # Out from Checking
                transactions.append(Transaction(
                    date=current, original_description='Chase Credit Crd Epay', amount=-payment_amt,
                    entity_id=entities['Chase Payment'].id, category_id=cats['Transfer CC Payment'].id, account_id=acc_checking.id
                ))
                # In to Credit Card
                transactions.append(Transaction(
                    date=current, original_description='Payment Thank You', amount=payment_amt,
                    entity_id=entities['Chase Payment'].id, category_id=cats['Transfer CC Payment'].id, account_id=acc_cc.id
                ))
            
            current += timedelta(days=1)

        # B. Variable Daily Spending (Groceries, Dining, Gas)
        for day in get_date_range(start_date, end_date):
            # Weekly Grocery Trip (Saturday)
            if day.weekday() == 5:
                amt = -1 * random.uniform(120, 200)
                transactions.append(Transaction(
                    date=day, original_description='Publix #1022', amount=amt,
                    entity_id=entities['Publix'].id, category_id=cats['Groceries'].id, account_id=acc_cc.id
                ))
            
            # Frequent Dining Out (30% chance any day)
            if random.random() < 0.3:
                amt = -1 * random.uniform(15, 45)
                transactions.append(Transaction(
                    date=day, original_description='McDonalds 992', amount=amt,
                    entity_id=entities['McDonalds'].id, category_id=cats['Eat Out'].id, account_id=acc_cc.id
                ))

            # Gas every ~10 days
            if day.day in [1, 11, 21]:
                amt = -1 * random.uniform(40, 60)
                transactions.append(Transaction(
                    date=day, original_description='Shell Oil 123', amount=amt,
                    entity_id=entities['Shell Oil'].id, category_id=cats['Transportation'].id, account_id=acc_cc.id
                ))

        # C. HSA Expenses (Medical Only)
        current = start_date
        while current <= end_date:
            if current.day == 14:
                amt = -1 * random.uniform(15, 50)
                transactions.append(Transaction(
                    date=current, original_description='CVS Pharmacy', amount=amt,
                    entity_id=entities['CVS Pharmacy'].id, category_id=cats['Medical'].id, account_id=acc_hsa.id
                ))
            
            # Quest Diagnostics (Quarterly)
            if current.day == 2 and current.month % 3 == 0:
                amt = -1 * random.uniform(100, 250)
                transactions.append(Transaction(
                    date=current, original_description='Quest Diagnostics', amount=amt,
                    entity_id=entities['Quest Diagnostics'].id, category_id=cats['Medical'].id, account_id=acc_hsa.id
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

    print("✅ Database seeded successfully with Entity model!")
    print(f"   Created {len(entities)} entities with auto-matching patterns")
    print("   You can now run 'python app.py'")

if __name__ == '__main__':
    seed_database()
