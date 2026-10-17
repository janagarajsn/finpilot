-- Sum of every transaction in the reporting currency (balance = opening balances + this).
-- :as_of NULL = all transactions (dashboard); a date = balance at the end of that day (forecasts).
-- params: :u, :as_of
SELECT coalesce(sum(amount_reporting), 0) AS net, count(*) FILTER (WHERE amount_reporting IS NULL) AS missing
FROM v_transactions_reporting
WHERE user_id = :u AND (CAST(:as_of AS date) IS NULL OR date <= CAST(:as_of AS date));
