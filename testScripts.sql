-- =============================================
-- 1. CLEANUP (Clears existing data to start fresh)
-- =============================================
TRUNCATE TABLE 
    statement_record, 
    budget, 
    event, 
    transaction, 
    payee, 
    payee_rule, 
    category, 
    account 
RESTART IDENTITY CASCADE;

-- =============================================
-- 2. ACCOUNTS
-- =============================================
INSERT INTO account (id, name, account_type, starting_balance) VALUES
(1, 'Wells Fargo Checking', 'checking', 5000.00),
(2, 'Chase Sapphire', 'credit_card', 0.00),
(3, 'High Yield Savings', 'savings', 20000.00),
(4, 'HealthEquity HSA', 'hsa', 1500.00);

-- =============================================
-- 3. CATEGORIES
-- =============================================
-- Income
INSERT INTO category (id, name, type) VALUES (1, 'Salary', 'Income');
INSERT INTO category (id, name, type) VALUES (2, 'Bonus', 'Income');
INSERT INTO category (id, name, type) VALUES (3, 'Rental Income', 'Income');
INSERT INTO category (id, name, type) VALUES (4, 'Interest Income', 'Income');

-- Expenses (Core)
INSERT INTO category (id, name, type) VALUES (10, 'Groceries', 'Expense');
INSERT INTO category (id, name, type) VALUES (11, 'Eat Out', 'Expense');
INSERT INTO category (id, name, type) VALUES (12, 'Housing', 'Expense');
INSERT INTO category (id, name, type) VALUES (13, 'Utilities', 'Expense');
INSERT INTO category (id, name, type) VALUES (14, 'Transportation', 'Expense');
INSERT INTO category (id, name, type) VALUES (15, 'Medical', 'Expense');
INSERT INTO category (id, name, type) VALUES (16, 'Entertainment', 'Expense');
INSERT INTO category (id, name, type) VALUES (17, 'Shopping', 'Expense');

-- Expenses (Strategic/Large - Excluded from Core Operating)
INSERT INTO category (id, name, type) VALUES (30, 'Car Payment', 'Expense');
INSERT INTO category (id, name, type) VALUES (31, 'Insurance', 'Expense');

-- Transfers
INSERT INTO category (id, name, type) VALUES (50, 'Transfer Checking->Savings', 'Transfer');
INSERT INTO category (id, name, type) VALUES (51, 'Transfer Savings->Checking', 'Transfer');
INSERT INTO category (id, name, type) VALUES (52, 'Transfer Fidelity', 'Transfer'); 
INSERT INTO category (id, name, type) VALUES (53, 'Transfer Credit Card Payment', 'Transfer');
INSERT INTO category (id, name, type) VALUES (54, 'Transfer Money Market', 'Transfer');
INSERT INTO category (id, name, type) VALUES (55, 'Strategic Transfer', 'Transfer');

-- Default
INSERT INTO category (id, name, type) VALUES (99, 'Uncategorized', 'Expense');

-- =============================================
-- 4. PAYEE RULES
-- =============================================
INSERT INTO payee_rule (id, fragment, display_name, category_id) VALUES
(1, 'PUBLIX', 'Publix', 10),
(2, 'KROGER', 'Kroger', 10),
(3, 'WHOLE FDS', 'Whole Foods', 10),
(4, 'MCDONALDS', 'McDonalds', 11),
(5, 'CHIPOTLE', 'Chipotle', 11),
(6, 'STARBUCKS', 'Starbucks', 11),
(7, 'SHELL', 'Shell Oil', 14),
(8, 'EXXON', 'Exxon', 14),
(9, 'NETFLIX', 'Netflix', 16),
(10, 'SPOTIFY', 'Spotify', 16),
(11, 'AMZN', 'Amazon', 17),
(12, 'TARGET', 'Target', 17),
(13, 'PAYROLL', 'My Employer', 1),
(14, 'CHASE CREDIT CRD', 'Chase Auto-Pay', 53),
(15, 'FIDELITY INVEST', 'Fidelity', 52),
(16, 'STATE FARM', 'State Farm', 31),
(17, 'TOYOTA FIN', 'Toyota Financial', 30),
(18, 'DOMINION POWER', 'Dominion Energy', 13),
(19, 'VERIZON', 'Verizon Wireless', 13),
(20, 'CVS', 'CVS Pharmacy', 15),
(21, 'QUEST DIAG', 'Quest Diagnostics', 15),
(22, 'DENTIST', 'Family Dentist', 15);

-- =============================================
-- 5. PAYEES
-- =============================================
INSERT INTO payee (id, name, rule_id) VALUES
(1, 'Publix #1022 Jacksonville', 1),
(2, 'Kroger 443 Atlanta', 2),
(3, 'McDonalds 992', 4),
(4, 'Shell Oil 123123', 7),
(5, 'Amazon.com*8823', 11),
(6, 'Direct Dep Payroll 9922', 13),
(7, 'Chase Credit Crd Epay', 14),
(8, 'Fidelity Brokerage Transfer', 15),
(9, 'Toyota Financial Svc', 17),
(10, 'State Farm Insurance', 16),
(11, 'Dominion Power Bill', 18),
(12, 'Verizon Wireless', 19),
(13, 'Unknown Check 1001', NULL),
(14, 'CVS Pharmacy #9922', 20),
(15, 'Quest Diagnostics 882', 21),
(16, 'Happy Teeth Dentist', 22);

-- =============================================
-- 6. SAVED BUDGETS
-- =============================================
-- Create a default view for "Core Living Expenses" (Groceries + Dining + Housing + Utilities)
INSERT INTO budget (name, criteria, start_date, end_date) VALUES 
('Core Living Expenses', 
 '{"category_ids": ["10", "11", "12", "13"], "payee_names": [], "account_ids": []}', 
 '2024-01-01', 
 '2024-12-31');

-- =============================================
-- 7. EVENTS
-- =============================================
INSERT INTO event (date, description) VALUES
('2024-01-15', 'Started New Job'),
('2024-06-01', 'Bought New Car'),
('2025-01-01', 'New Years Resolution: Save More'),
('2025-08-15', 'Paid Off Car!');

-- =============================================
-- 8. TRANSACTIONS (Generating ~2 Years of Data)
-- =============================================

-- A. RECURRING MONTHLY INCOME (Salary)
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Direct Dep Payroll 9922', 4000.00, 6, 1, 1, false FROM generate_series('2024-01-01'::date, '2025-12-01'::date, '1 month') as d;

-- B. RECURRING BILLS
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Dominion Power Bill', -150.00, 11, 13, 1, false FROM generate_series('2024-01-05'::date, '2025-12-05'::date, '1 month') as d;

INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Verizon Wireless', -120.00, 12, 13, 1, false FROM generate_series('2024-01-08'::date, '2025-12-08'::date, '1 month') as d;

-- C. SAVINGS TRANSFERS
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Transfer to Savings', -500.00, 13, 50, 1, false FROM generate_series('2024-01-10'::date, '2025-12-10'::date, '1 month') as d;

INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Transfer from Checking', 500.00, 13, 50, 3, false FROM generate_series('2024-01-10'::date, '2025-12-10'::date, '1 month') as d;

-- D. CAR PAYMENT (Stops Aug 2025)
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Toyota Financial Svc', -450.00, 9, 30, 1, false FROM generate_series('2024-01-20'::date, '2025-07-20'::date, '1 month') as d;

-- E. GROCERIES & DINING (Credit Card)
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Publix #1022 Jacksonville', -1 * (150 + floor(random() * 50)), 1, 10, 2, false FROM generate_series('2024-01-02'::date, '2025-12-28'::date, '1 week') as d;

INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date + (floor(random()*3)::int), 'McDonalds 992', -1 * (15 + floor(random() * 20)), 3, 11, 2, false FROM generate_series('2024-01-01'::date, '2025-12-28'::date, '5 days') as d;

-- F. CREDIT CARD PAYMENTS
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Chase Credit Crd Epay', -2500.00, 7, 53, 1, false FROM generate_series('2024-01-28'::date, '2025-12-28'::date, '1 month') as d;

INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Payment Thank You', 2500.00, 7, 53, 2, false FROM generate_series('2024-01-28'::date, '2025-12-28'::date, '1 month') as d;

-- G. HSA MEDICAL EXPENSES (Account ID 4) - This tests the new HSA Chart
INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'CVS Pharmacy #9922', -1 * (20 + floor(random() * 30)), 14, 15, 4, false FROM generate_series('2024-01-15'::date, '2025-12-15'::date, '1 month') as d;

INSERT INTO transaction (date, original_description, amount, payee_id, category_id, account_id, is_deleted)
SELECT d::date, 'Quest Diagnostics', -1 * (50 + floor(random() * 100)), 15, 15, 4, false FROM generate_series('2024-03-01'::date, '2025-09-01'::date, '3 months') as d;

-- =============================================
-- 9. RESET SEQUENCES
-- =============================================
SELECT setval('account_id_seq', (SELECT MAX(id) FROM account));
SELECT setval('category_id_seq', (SELECT MAX(id) FROM category));
SELECT setval('payee_rule_id_seq', (SELECT MAX(id) FROM payee_rule));
SELECT setval('payee_id_seq', (SELECT MAX(id) FROM payee));
SELECT setval('transaction_id_seq', (SELECT MAX(id) FROM transaction));
SELECT setval('event_id_seq', (SELECT MAX(id) FROM event));
SELECT setval('budget_id_seq', (SELECT MAX(id) FROM budget));