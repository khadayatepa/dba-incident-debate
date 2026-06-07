"""
Create the incident diagnostic snapshot and the past-incident knowledge base
(with real OpenAI embeddings) — entirely through the SQLcl MCP server.

This seeds a *captured* incident so the debate is reproducible and safe: we do NOT
cause real load on your database. The agents read these snapshot tables exactly as
they would read v$session / v$sql during a live incident.

Run:  python src/seed.py
"""
from __future__ import annotations

import asyncio

from openai import AsyncOpenAI

import config
from mcp_oracle import open_oracle_mcp, OracleMCP
from tools import _vector_literal

INCIDENT_ID = 5001

INCIDENT = (
    INCIDENT_ID,
    "Severe app slowdown — sessions piling up on row-lock contention",
    "SEV1",
    "Order-processing API timing out for ~15 minutes. Active sessions spiking, most "
    "stuck waiting on 'enq: TX - row lock contention'. Suspected a single blocking "
    "session holding a lock from an uncommitted transaction.",
)

# metric_name, metric_value, normal_range, note
METRICS = [
    ("active_sessions", "142", "15-25", "≈6x normal"),
    ("blocked_sessions", "58", "0-1", "almost all waiters trace to one blocker"),
    ("host_cpu_pct", "71", "20-45", "elevated but not saturated"),
    ("top_wait_event", "enq: TX - row lock contention", "n/a", "dominant wait"),
    ("avg_api_response_ms", "8500", "150-300", "user-visible timeouts"),
]

# sid, serial, username, status, sql_id, event, blocking_session, secs_in_wait, module
SESSIONS = [
    (123, 44021, "BATCHJOB", "INACTIVE", "b9x1", "SQL*Net message from client", None, 1320,
     "NightlyBatch"),  # the ROOT BLOCKER: idle in transaction, holding a row lock
    (312, 9001, "APPUSR", "ACTIVE", "a1q7", "enq: TX - row lock contention", 123, 495, "OrderService"),
    (318, 9002, "APPUSR", "ACTIVE", "a1q7", "enq: TX - row lock contention", 123, 470, "OrderService"),
    (327, 9003, "APPUSR", "ACTIVE", "a1q7", "enq: TX - row lock contention", 123, 441, "OrderService"),
    (340, 9004, "APPUSR", "ACTIVE", "a1q7", "enq: TX - row lock contention", 123, 388, "OrderService"),
    (355, 9005, "APPUSR", "ACTIVE", "a1q7", "enq: TX - row lock contention", 123, 350, "CheckoutAPI"),
    (361, 9006, "APPUSR", "ACTIVE", "c4z2", "db file scattered read", None, 5, "ReportingJob"),
]

# sql_id, sql_text, executions, elapsed_sec_per_exec, plan_hash, note
SQLS = [
    ("b9x1", "UPDATE orders SET status = :1 WHERE order_id = :2", 1, 0.02, 1182937,
     "the blocker's statement — committed? no; session now idle holding the row lock"),
    ("a1q7", "UPDATE orders SET status = :1, updated_at = SYSTIMESTAMP WHERE order_id = :2",
     5800, 0.01, 1182937, "waiters want the same row(s) the blocker locked"),
    ("c4z2", "SELECT /*+ FULL(o) */ * FROM orders o WHERE updated_at > :1", 12, 6.4, 2284411,
     "unrelated full scan; adds CPU but not the cause"),
]

# Past-incident knowledge base: title, category, symptom_text, resolution_text
PAST = [
    ("Idle blocker holding row lock stalls order API", "Locking",
     "Many sessions waiting on 'enq: TX - row lock contention'; traced to one idle session "
     "(a stuck batch job) holding a row lock from an uncommitted transaction.",
     "Identified the blocker via BLOCKING_SESSION, confirmed with the batch owner that the job "
     "had stalled, then terminated the blocking session. Its transaction was small and rolled "
     "back in seconds; the waiters cleared immediately. Added a watchdog to alert on long "
     "uncommitted transactions."),
    ("High CPU after stats refresh changed a plan", "CPU/Plan",
     "Sudden high CPU and slow critical queries shortly after an optimizer statistics refresh.",
     "Confirmed a plan flip via plan_hash_value history, pinned the previous good plan with a "
     "SQL plan baseline, then re-gathered representative statistics off-peak."),
    ("Tablespace full causing ORA-01653", "Space",
     "Application errors: ORA-01653 unable to extend segment; a tablespace had no free space.",
     "Enabled autoextend and added a datafile to restore service, then scheduled cleanup and "
     "partition archiving to reclaim space."),
    ("Library cache contention from hard parsing", "Parsing",
     "Library cache / shared pool latch contention caused by excessive hard parsing because the "
     "application used literals instead of bind variables.",
     "Set cursor_sharing=FORCE as a stopgap to share cursors, and worked with developers to "
     "introduce bind variables in the hot code path."),
    ("log file sync waits during peak load", "Redo",
     "Frequent 'log file sync' waits and commit latency during peak load due to undersized redo "
     "logs on slow storage.",
     "Added larger redo log groups on faster storage and tuned commit batching; sync waits "
     "dropped back to normal."),
    ("Deadlock between two app modules (ORA-00060)", "Locking",
     "Intermittent ORA-00060 deadlocks between two modules updating the same tables in different "
     "order.",
     "Standardized the lock-acquisition order across modules and added bounded retry-on-deadlock "
     "logic; deadlocks stopped recurring."),
]


def _q(text: str) -> str:
    t = (text or "").replace("'", "''")
    if "&" in t:
        return "'" + t.replace("&", "'||CHR(38)||'") + "'"
    return "'" + t + "'"


def _num(v) -> str:
    return "NULL" if v is None else str(v)


async def embed_all(client: AsyncOpenAI, texts: list[str]) -> list[list[float]]:
    resp = await client.embeddings.create(model=config.EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


CREATE = [
    "CREATE TABLE incidents (incident_id NUMBER PRIMARY KEY, title VARCHAR2(200), "
    "severity VARCHAR2(20), opened_at TIMESTAMP DEFAULT SYSTIMESTAMP, symptom_text VARCHAR2(4000))",
    "CREATE TABLE incident_metrics (incident_id NUMBER, metric_name VARCHAR2(60), "
    "metric_value VARCHAR2(200), normal_range VARCHAR2(60), note VARCHAR2(400))",
    "CREATE TABLE session_snapshot (incident_id NUMBER, sid NUMBER, serial# NUMBER, "
    "username VARCHAR2(40), status VARCHAR2(12), sql_id VARCHAR2(20), event VARCHAR2(120), "
    "blocking_session NUMBER, secs_in_wait NUMBER, module VARCHAR2(60))",
    "CREATE TABLE sql_snapshot (incident_id NUMBER, sql_id VARCHAR2(20), sql_text VARCHAR2(1000), "
    "executions NUMBER, elapsed_sec_per_exec NUMBER, plan_hash NUMBER, note VARCHAR2(400))",
    f"CREATE TABLE past_incidents (past_id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
    f"title VARCHAR2(200), category VARCHAR2(40), symptom_text VARCHAR2(4000), "
    f"resolution_text VARCHAR2(4000), embedding VECTOR({config.EMBED_DIM}, FLOAT32))",
]


async def _run(mcp: OracleMCP, sql: str, what: str) -> None:
    out = await mcp.run_sql(sql)
    if "ORA-" in out or "Error" in out or "cancelled" in out:
        ora = next((ln for ln in out.splitlines() if "ORA-" in ln), out[:400])
        raise RuntimeError(f"{what} FAILED:\n{ora}")
    print(f"  · {what}")


async def main() -> None:
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY or None)
    print(f"Embedding {len(PAST)} past incidents with {config.EMBED_MODEL} ...")
    vectors = await embed_all(client, [f"{t}. {s}" for (t, _c, s, _r) in PAST])

    async with open_oracle_mcp(config.SQLCL_COMMAND, config.ORACLE_MCP_CONNECTION) as mcp:
        print(f"Connected via SQLcl MCP (connection '{config.ORACLE_MCP_CONNECTION}').\n")
        await mcp.run_sqlcl("set define off")

        print("Creating tables ...")
        for tbl in ("INCIDENTS", "INCIDENT_METRICS", "SESSION_SNAPSHOT", "SQL_SNAPSHOT", "PAST_INCIDENTS"):
            await mcp.run_sql(
                f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {tbl} CASCADE CONSTRAINTS'; "
                "EXCEPTION WHEN OTHERS THEN NULL; END;"
            )
        for ddl in CREATE:
            await _run(mcp, ddl, ddl.split()[2])

        print("Loading incident snapshot ...")
        iid, title, sev, sym = INCIDENT
        await _run(mcp,
            f"INSERT INTO incidents (incident_id,title,severity,symptom_text) "
            f"VALUES ({iid},{_q(title)},{_q(sev)},{_q(sym)})", "incident")

        rows = "\n".join(
            f"  INTO incident_metrics VALUES ({iid},{_q(m)},{_q(v)},{_q(nr)},{_q(nt)})"
            for (m, v, nr, nt) in METRICS)
        await _run(mcp, f"INSERT ALL\n{rows}\nSELECT 1 FROM DUAL", "metrics")

        rows = "\n".join(
            f"  INTO session_snapshot VALUES ({iid},{sid},{ser},{_q(u)},{_q(st)},{_q(sq)},{_q(ev)},{_num(bl)},{sw},{_q(mod)})"
            for (sid, ser, u, st, sq, ev, bl, sw, mod) in SESSIONS)
        await _run(mcp, f"INSERT ALL\n{rows}\nSELECT 1 FROM DUAL", "session snapshot")

        rows = "\n".join(
            f"  INTO sql_snapshot VALUES ({iid},{_q(sid)},{_q(txt)},{ex},{el},{ph},{_q(nt)})"
            for (sid, txt, ex, el, ph, nt) in SQLS)
        await _run(mcp, f"INSERT ALL\n{rows}\nSELECT 1 FROM DUAL", "sql snapshot")

        print("Loading past-incident knowledge base + embeddings ...")
        for (t, c, s, r), vec in zip(PAST, vectors):
            await mcp.run_sql(
                "INSERT INTO past_incidents (title,category,symptom_text,resolution_text,embedding) "
                f"VALUES ({_q(t)},{_q(c)},{_q(s)},{_q(r)},TO_VECTOR('{_vector_literal(vec)}'))")
        print(f"  · {len(PAST)} past incidents loaded")

        print("Building HNSW vector index ...")
        try:
            await mcp.run_sql("DROP INDEX past_incidents_hnsw_idx")
        except Exception:
            pass
        try:
            await mcp.run_sql(
                "CREATE VECTOR INDEX past_incidents_hnsw_idx ON past_incidents (embedding) "
                "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95")
            print("  · index created")
        except Exception as exc:
            print(f"  ! Skipping vector index ({exc}). Exact search will be used.")

        await mcp.run_sql("COMMIT")
        print("\nDone. Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
