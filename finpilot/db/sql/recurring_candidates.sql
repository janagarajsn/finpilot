-- Recurring-detection candidates: one row per (merchant, direction, original currency) up to
-- :as_of (single charges included: a misspelled descriptor still joins its series in Python), occurrences ordered by date. Amounts are ORIGINAL amounts (a $20
-- subscription is a fixed $20 series even if its INR cost moves with the rate).
-- params: :u, :as_of, :since
SELECT t.merchant_norm,
       CASE WHEN t.amount < 0 THEN 'expense' ELSE 'income' END AS kind,
       t.currency,
       array_agg(t.id ORDER BY t.date, t.id)          AS ids,
       array_agg(t.date ORDER BY t.date, t.id)        AS dates,
       array_agg(abs(t.amount) ORDER BY t.date, t.id) AS amounts,
       (array_agg(t.raw_description ORDER BY t.date DESC, t.id DESC))[1] AS last_description,
       (array_agg(t.category_id ORDER BY t.date DESC, t.id DESC)
          FILTER (WHERE t.category_id IS NOT NULL))[1]                    AS category_id,
       (array_agg(t.recurring_rule_id ORDER BY t.date DESC)
          FILTER (WHERE t.recurring_rule_id IS NOT NULL))[1]              AS rule_id
FROM transactions t
WHERE t.user_id = :u
  AND t.merchant_norm IS NOT NULL
  AND t.date BETWEEN :since AND :as_of
GROUP BY t.merchant_norm, CASE WHEN t.amount < 0 THEN 'expense' ELSE 'income' END, t.currency;
