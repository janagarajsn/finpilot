-- Income / spend / savings totals for one user and date range, in the reporting currency.
-- Transfers between own accounts are neither income nor spend; savings moves are reported apart.
-- params: :u (user id), :start, :end (inclusive dates)
WITH t AS (
  SELECT v.amount_reporting AS amt, c.flow_type
  FROM v_transactions_reporting v
  LEFT JOIN categories c ON c.id = v.category_id
  WHERE v.user_id = :u AND v.date BETWEEN :start AND :end
)
SELECT
  coalesce(sum(amt) FILTER (WHERE amt > 0 AND coalesce(flow_type, 'income') NOT IN ('transfer', 'savings')), 0) AS income,
  coalesce(-sum(amt) FILTER (WHERE amt < 0 AND coalesce(flow_type, 'expense') NOT IN ('transfer', 'savings')), 0) AS spend,
  coalesce(-sum(amt) FILTER (WHERE flow_type = 'savings'), 0) AS saved,
  count(*) AS txn_count,
  count(*) FILTER (WHERE amt IS NULL) AS missing_rate_count
FROM t;
