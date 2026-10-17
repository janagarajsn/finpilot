-- Monthly spend per category (reporting currency), zero-filled over every month in range, for
-- MoM / trailing-3-month deltas and robust category-spike baselines. Expenses only; transfers and
-- savings excluded. params: :u, :start, :as_of
WITH months AS (
  SELECT generate_series(date_trunc('month', CAST(:start AS date)),
                         date_trunc('month', CAST(:as_of AS date)), interval '1 month')::date AS month
), spend AS (
  SELECT date_trunc('month', v.date)::date AS month, v.category_id,
         -sum(v.amount_reporting) AS spend, count(*) AS n
  FROM v_transactions_reporting v
  LEFT JOIN categories c ON c.id = v.category_id
  WHERE v.user_id = :u AND v.date BETWEEN :start AND :as_of AND v.amount_reporting < 0
    AND coalesce(c.flow_type, 'expense') NOT IN ('transfer', 'savings')
  GROUP BY 1, 2
), cats AS (
  SELECT DISTINCT s.category_id, coalesce(c.name, 'Uncategorized') AS name, c.slug, c.nature
  FROM spend s LEFT JOIN categories c ON c.id = s.category_id
)
SELECT m.month, k.category_id, k.name, k.slug, k.nature,
       coalesce(s.spend, 0) AS spend, coalesce(s.n, 0) AS n
FROM months m CROSS JOIN cats k
LEFT JOIN spend s ON s.month = m.month AND s.category_id IS NOT DISTINCT FROM k.category_id
ORDER BY k.name, m.month;
