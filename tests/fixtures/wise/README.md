# Synthetic Wise statements

Two monthly statements of the EUR Wise balance, in the column layout of Wise's balance statement CSV, newest row first as Wise writes them. Everything is invented. Used by the bank import tests (#56) and AC-7.

| File | Month | Opening | Closing | Rows |
|---|---|---|---|---|
| `statement_EUR_2026-10.csv` | October 2026 | 12,500.00 | 14,209.86 | 6 |
| `statement_EUR_2026-11.csv` | November 2026 | 14,209.86 | 14,458.36 | 3 |

November opens with October's closing balance, so importing both in order adds up. The tests change single cells to make balances that do not.
