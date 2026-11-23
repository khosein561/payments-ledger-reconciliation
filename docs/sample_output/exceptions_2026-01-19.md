# Reconciliation exceptions as of 2026-01-19

Open breaks: **19**

## By type

| Type | Count | Total USD |
|---|---|---|
| amount_mismatch | 4 | 973.74 |
| duplicate | 3 | 671.20 |
| missing_at_bank | 2 | 29,988.42 |
| missing_in_ledger | 3 | 249.92 |
| missing_in_processor | 3 | 788.36 |
| timing_difference | 4 | 1,013.07 |

## By age

| Age | Count |
|---|---|
| 0-1d | 1 |
| 2-3d | 2 |
| 4-7d | 4 |
| 8d+ | 12 |

## Oldest / largest (top 20)

| Type | Ref | USD | Age (d) | Severity | Suggested action |
|---|---|---|---|---|---|
| duplicate | ch_9c96e94d71c366 | 466.07 | 13 | medium | Confirm the duplicate is a file artefact, not a double charge; ask the processor to re-issue the file. |
| missing_in_processor | ch_839fbc1223b513 | 221.54 | 13 | high | Confirm the transaction exists in the processor dashboard; if not, reverse the ledger entry. |
| missing_in_ledger | ch_ghost_0002 | 171.13 | 13 | high | Check webhook delivery logs for the transaction; replay the event if it was missed. |
| missing_at_bank | po_ab3b4d560c95ee | 15107.64 | 12 | high | Ask the bank / treasury whether the payout is delayed; escalate if older than 3 days. |
| amount_mismatch | ch_4bb00fb27c4026 | 268.71 | 12 | high | Compare ledger and processor amounts; check for partial refunds or fee changes. |
| timing_difference | ch_0bd4a90640be0f | 101.56 | 12 | low | Usually benign; watch for a pattern and confirm it settles within the expected window. |
| timing_difference | ch_d42629e43e4288 | 421.53 | 11 | low | Usually benign; watch for a pattern and confirm it settles within the expected window. |
| missing_at_bank | po_8d4a75551ac8ea | 14880.78 | 10 | high | Ask the bank / treasury whether the payout is delayed; escalate if older than 3 days. |
| duplicate | ch_85c750c693da11 | 152.9 | 10 | medium | Confirm the duplicate is a file artefact, not a double charge; ask the processor to re-issue the file. |
| amount_mismatch | ch_bee3eb1d181ee9 | 349.77 | 8 | high | Compare ledger and processor amounts; check for partial refunds or fee changes. |
| missing_in_ledger | ch_ghost_0001 | 31.41 | 8 | high | Check webhook delivery logs for the transaction; replay the event if it was missed. |
| amount_mismatch | ch_5edb0db0b63bcf | 26.44 | 8 | high | Compare ledger and processor amounts; check for partial refunds or fee changes. |
| amount_mismatch | ch_c1235c76c5a0dc | 328.82 | 7 | high | Compare ledger and processor amounts; check for partial refunds or fee changes. |
| duplicate | ch_dc9c9665a674c2 | 52.23 | 7 | medium | Confirm the duplicate is a file artefact, not a double charge; ask the processor to re-issue the file. |
| timing_difference | ch_737d1fd11376e0 | 150.51 | 6 | low | Usually benign; watch for a pattern and confirm it settles within the expected window. |
| missing_in_processor | ch_1e6a669c6d52d5 | 450.9 | 4 | high | Confirm the transaction exists in the processor dashboard; if not, reverse the ledger entry. |
| timing_difference | ch_cf0671720b274b | 339.47 | 3 | low | Usually benign; watch for a pattern and confirm it settles within the expected window. |
| missing_in_processor | ch_b8667723d4526b | 115.92 | 2 | high | Confirm the transaction exists in the processor dashboard; if not, reverse the ledger entry. |
| missing_in_ledger | ch_ghost_0000 | 47.38 | 1 | high | Check webhook delivery logs for the transaction; replay the event if it was missed. |
