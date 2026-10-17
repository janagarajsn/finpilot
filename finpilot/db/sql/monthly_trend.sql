-- Month-by-month income and spend for the last :months months (zero-filled), reporting currency.
-- params: :u, :end (date; last month included), :months (int)
WITH months AS (
  SELECT generate_series(
           date_trunc('month', CAST(:end AS date)) - make_interval(months => CAST(:months AS int) - 1),
           date_trunc('month', CAST(:end AS date)),
           interval '1 month')::date AS month
),
t AS (
  SELECT date_trunc('month', v.date)::date AS month, v.amount_reporting AS amt, c.flow_type
  FROM v_transactions_reporting v
  LEFT JOIN categories c ON c.id = v.category_id
  WHERE v.user_id = :u
    AND v.date >= date_trunc('month', CAST(:end AS date)) - make_interval(months => CAST(:months AS int) - 1)
    AND v.date <= CAST(:end AS date)
)
SELECT m.month,
  coalesce(sum(t.amt) FILTER (WHERE t.amt > 0 AND coalesce(t.flow_type, 'income') NOT IN ('transfer', 'savings')), 0) AS income,
  coalesce(-sum(t.amt) FILTER (WHERE t.amt < 0 AND coalesce(t.flow_type, 'expense') NOT IN ('transfer', 'savings')), 0) AS spend
FROM months m
LEFT JOIN t ON t.month = m.month
GROUP BY m.month
ORDER BY m.month;
