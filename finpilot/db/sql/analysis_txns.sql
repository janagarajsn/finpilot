-- Analyzer input frame: transactions up to :as_of in the reporting currency with category semantics.
-- Uncategorized rows get flow_type from the sign and nature 'unknown'. params: :u, :start, :as_of
SELECT v.id, v.date, v.raw_description, v.merchant_norm, v.amount, v.currency,
       v.amount_reporting, v.reporting_currency, v.category_id, v.recurring_rule_id, v.source,
       c.slug AS category_slug,
       coalesce(c.name, 'Uncategorized') AS category_name,
       coalesce(c.flow_type, CASE WHEN v.amount > 0 THEN 'income' ELSE 'expense' END) AS flow_type,
       coalesce(c.nature, 'unknown') AS nature
FROM v_transactions_reporting v
LEFT JOIN categories c ON c.id = v.category_id
WHERE v.user_id = :u AND v.date BETWEEN :start AND :as_of
ORDER BY v.date, v.id;
