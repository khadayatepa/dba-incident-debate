# DBA Incident War-Room — Multi-Agent Debate over Oracle 26ai (SQLcl MCP + AI)

Three AI DBAs investigate a database incident over the **same** Oracle 26ai data —
reaching it **only** through the SQLcl MCP server — and debate how to fix it:

```
 🚑 Mitigator (restore fast)  ─┐
 🛡️ Guardian (risk/root cause) ─┤── read-only SQL + incident vector search ─> Oracle 26ai
 🧭 Incident Commander (Judge) ─┘
```

Mitigator pushes the fastest safe fix, Guardian challenges the risk and finds root
cause (citing similar past incidents via **AI Vector Search**), Mitigator refines,
and the Commander issues a remediation **runbook**.

> Same architecture as the credit-debate demo — different problem domain. It reuses
> the proven SQLcl MCP bridge and uses **separate table names** so both demos can
> live in the same schema.

## 🔒 Safety: the agents are read-only advisors
The agents may run **only `SELECT`** queries (enforced in `tools.py`). They never
KILL a session, ALTER, or run DML — any change is **recommended in the runbook** for
a human DBA to execute, tagged `[CHANGE — needs approval]`. The incident is a
*captured snapshot*, so running this never loads or destabilizes your database.

## Prerequisites
1. **Oracle Database 23ai / 26ai** with vectors enabled.
2. **SQLcl 25.2+** on `PATH` with a saved connection:
   ```
   sql /nolog
   SQL> conn -save DEBATE -savepwd debate@<your-tns-alias>
   ```
3. **Python 3.10+** and an **OpenAI API key**.

## Setup
```powershell
pip install -r requirements.txt
copy .env.example .env      # set OPENAI_API_KEY + ORACLE_MCP_CONNECTION
python src/seed.py          # creates incident snapshot + past-incident vectors
python src/debate.py        # runs Mitigator → Guardian → response → Commander runbook
streamlit run src/dashboard.py   # view incident debates in a local dashboard
```
Everything (seeding, debate, dashboard) goes through the SQLcl MCP server — no
`python-oracledb` or wallet config needed.

## Files
| File | Purpose |
| --- | --- |
| `sql/schema.sql` | Reference DDL for the incident snapshot + knowledge base. |
| `src/seed.py` | Loads a captured SEV1 incident + 6 past incidents (with embeddings) via MCP. |
| `src/mcp_oracle.py` | Async bridge to `sql -mcp` (shared with the credit demo). |
| `src/tools.py` | `run_sql` (SELECT-only, enforced) + `incident_search` (vector). |
| `src/persist.py` | Saves runs to `dba_debate_runs` / `dba_debate_arguments`. |
| `src/debate.py` | Orchestrates Mitigator / Guardian / response / Commander. |
| `src/dashboard_data.py` | Reads debates back out of Oracle as JSON. |
| `src/dashboard.py` | Streamlit dashboard over the persisted incident debates. |

## The seeded scenario (incident 5001)
A **SEV1**: the order API is timing out with ~140 active sessions, 58 of them blocked
on `enq: TX - row lock contention`. The blocking chain traces to **one idle session**
(`SID 123`, a stalled `BATCHJOB`) holding a row lock from an **uncommitted** UPDATE.

That's the tension the agents debate: Mitigator wants to kill SID 123 now to free 58
waiters; Guardian warns it's a batch job mid-transaction (rollback/consistency risk)
and — via vector search — finds the past incident where the safe play was *confirm
with the owner first, then kill*. The Commander turns that into an ordered runbook.

> ⚠️ A learning demo. The runbook is illustrative — review before running anything in
> production.
