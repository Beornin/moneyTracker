-- Backfill: categorize all existing brokerage transactions as "Investment Income"
-- and mark their entities as non-auto-created so they don't appear in the review queue.
-- Run once against budget_db after deploying the code change.

BEGIN;

-- 1. Update all brokerage transactions to Investment Income category
UPDATE transaction
SET category_id = (SELECT id FROM category WHERE name = 'Investment Income')
WHERE account_id IN (SELECT id FROM account WHERE account_type = 'brokerage')
  AND is_deleted = FALSE;

-- 2. Update entities that belong exclusively to brokerage transactions
--    so they are no longer flagged as auto-created (removes them from review count)
UPDATE entity
SET category_id  = (SELECT id FROM category WHERE name = 'Investment Income'),
    is_auto_created = FALSE
WHERE id IN (
    SELECT DISTINCT t.entity_id
    FROM transaction t
    JOIN account a ON a.id = t.account_id
    WHERE a.account_type = 'brokerage'
      AND t.is_deleted = FALSE
);

COMMIT;

-- Verify
SELECT
    a.name        AS account,
    cat.name      AS category,
    COUNT(t.id)   AS tx_count,
    SUM(t.amount) AS total_income
FROM transaction t
JOIN account  a   ON a.id   = t.account_id
JOIN category cat ON cat.id = t.category_id
WHERE a.account_type = 'brokerage'
  AND t.is_deleted   = FALSE
GROUP BY a.name, cat.name
ORDER BY a.name;
