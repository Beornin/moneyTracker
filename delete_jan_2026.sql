-- PostgreSQL Script to Remove All January 2026 Transactions and Statement Records
-- Run this to clean January 2026 data and allow re-upload

BEGIN;

-- Delete all transactions dated in January 2026
DELETE FROM transaction
WHERE date >= '2026-01-01' AND date <= '2026-01-31';

-- Delete all statement records that cover January 2026
-- This includes records where the period overlaps with January
DELETE FROM statement_record
WHERE (start_date >= '2026-01-01' AND start_date <= '2026-01-31')
   OR (end_date >= '2026-01-01' AND end_date <= '2026-01-31')
   OR (start_date <= '2026-01-01' AND end_date >= '2026-01-31');

-- Show counts of what was deleted
-- Uncomment the following lines to see the impact before committing:
-- SELECT COUNT(*) as deleted_transactions FROM transaction WHERE date >= '2026-01-01' AND date <= '2026-01-31';
-- SELECT COUNT(*) as deleted_statement_records FROM statement_record WHERE (start_date >= '2026-01-01' AND start_date <= '2026-01-31') OR (end_date >= '2026-01-01' AND end_date <= '2026-01-31') OR (start_date <= '2026-01-01' AND end_date >= '2026-01-31');

-- Commit the changes
COMMIT;

-- To rollback instead of commit, replace COMMIT; with ROLLBACK;
