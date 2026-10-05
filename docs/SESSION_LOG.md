
## 2026-10-05 — Semis cluster fully live

- All 4 HIP-3 pairs open: GOLD/SILVER, NVDA/PLTR, MU/TSM, AMD/TSM
- 10/10 concurrent position slots filled (crypto 6 + HIP-3 4)
- Scaler now correctly sums main+xyz dex balances ($3101.13)
- Daily order counter reset; symlinked repo→runtime to prevent future mismatches
- Fixed 11 discrete bugs this session (executor, reconcile, scaler, HL filter, etc.)
- Open observations:
  - TSM short combined ~$214 (from MU/TSM + AMD/TSM sharing the same leg)
  - AMD/TSM potentially partial-filled on the AMD leg (~$63 vs ~$122 expected)
  - Both low-risk, both queued for tomorrow
