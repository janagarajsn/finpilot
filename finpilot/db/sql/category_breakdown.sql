-- Spend per category for a date range (expenses only; transfers/savings excluded), reporting currency.
-- params: :u, :start, :end
SELECT c.id AS category_id,
       coalesce(c.name, 'Uncategorized') AS name,
       coalesce(c.color, '#cbd5e1') AS color,
       c.nature,
       -sum(v.amount_reporting) AS spend,
       count(*) AS txn_count
FROM v_transactions_reporting v
LEFT JOIN categories c ON c.id = v.category_id
WHERE v.user_id = :u AND v.date BETWEEN :start AND :end
  AND v.amount_reporting < 0
  AND coalesce(c.flow_type, 'expense') NOT IN ('transfer', 'savings')
GROUP BY c.id, c.name, c.color, c.nature
ORDER BY spend DESC;
