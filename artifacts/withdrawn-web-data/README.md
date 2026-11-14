# Withdrawn website data (moved out of `web/data/` on 2026-10-25)

These files were served by the dashboard before the October 2026 functional audit
(`reports/website-functional-audit-2026-10-25/AUDIT.md`). They are kept for the record
and are no longer used or served.

| File | Why it is withdrawn |
|---|---|
| `carbon_trace.json` | 2026-09-07 capture through the Electricity Maps Azure-region lookup, which returned the Indian zone IN-SO for both US regions. Not PJM/BPAT data. |
| `real_pilot.json` | Receipt of the `real-milp-pilot-v2` pilot (2026-09-07): its wrk2 script failed to load (`module 'socket' not found`), so latency and errors are a `GET /` measurement, and its carbon values come from the trace above. |
| `runs_summary.json` | `artifacts/runs/summary.csv` at the time: demo and dry-run rows plus the withdrawn pilot; not comparable measured policy results. |

The dashboard now shows recorded results from `web/data/evidence.json`, produced by
`scripts/export_web_evidence.py` from the 2026-10-12 live AKS session (gate v2.1) and the
offline evaluation.
