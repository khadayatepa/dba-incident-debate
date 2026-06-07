"""
Persist DBA debate runs to Oracle (via MCP) so a dashboard can read them.

Uses table names prefixed `dba_` so it never collides with the credit-debate demo
in the same schema. Long agent text exceeds the 4000-byte SQL literal limit, so
CLOBs are built from <=1500-char TO_CLOB(...) chunks; '&' becomes CHR(38) and
newlines become CHR(10) (SQLcl treats both specially in transit).
"""
from __future__ import annotations

from mcp_oracle import OracleMCP


def _create_if_absent(ddl: str) -> str:
    body = ddl.replace("'", "''")
    return (
        "BEGIN EXECUTE IMMEDIATE '" + body + "'; "
        "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF; END;"
    )


DDL = [
    _create_if_absent(
        "CREATE TABLE dba_debate_runs (run_id NUMBER PRIMARY KEY, incident_id NUMBER, "
        "created_at TIMESTAMP DEFAULT SYSTIMESTAMP, model VARCHAR2(60), runbook CLOB)"
    ),
    _create_if_absent(
        "CREATE TABLE dba_debate_arguments (run_id NUMBER, seq NUMBER, phase VARCHAR2(30), "
        "persona VARCHAR2(20), content CLOB, created_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
    ),
    "CREATE OR REPLACE VIEW v_dba_debate_feed AS "
    "SELECT r.run_id, r.incident_id, i.title AS incident_title, r.created_at, r.model, "
    "a.seq, a.phase, a.persona, a.content AS argument, r.runbook "
    "FROM dba_debate_runs r JOIN dba_debate_arguments a ON a.run_id = r.run_id "
    "LEFT JOIN incidents i ON i.incident_id = r.incident_id",
]


def _q(text: str) -> str:
    t = (text or "").replace("'", "''")
    t = t.replace("&", "'||CHR(38)||'")
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "'||CHR(10)||'")
    return "'" + t + "'"


def _clob_expr(text: str, size: int = 1500) -> str:
    raw = text or ""
    chunks = [raw[i : i + size] for i in range(0, len(raw), size)] or [""]
    return "||".join("TO_CLOB(" + _q(c) + ")" for c in chunks)


async def _exec(mcp: OracleMCP, sql: str, what: str) -> None:
    out = await mcp.run_sql(sql)
    if "ORA-" in out or "Error" in out or "cancelled" in out:
        ora = next((ln for ln in out.splitlines() if "ORA-" in ln), out[:300])
        raise RuntimeError(f"persist {what} FAILED: {ora}")


async def ensure_tables(mcp: OracleMCP) -> None:
    for stmt in DDL:
        await mcp.run_sql(stmt)


async def save_debate(
    mcp: OracleMCP,
    *,
    run_id: int,
    incident_id: int,
    model: str,
    arguments: list[tuple[int, str, str, str]],  # (seq, phase, persona, content)
    runbook: str,
) -> None:
    await _exec(
        mcp,
        "INSERT INTO dba_debate_runs (run_id, incident_id, model, runbook) VALUES "
        f"({run_id}, {incident_id}, {_q(model)}, {_clob_expr(runbook)})",
        "insert run",
    )
    for seq, phase, persona, content in arguments:
        await _exec(
            mcp,
            "INSERT INTO dba_debate_arguments (run_id, seq, phase, persona, content) VALUES "
            f"({run_id}, {seq}, {_q(phase)}, {_q(persona)}, {_clob_expr(content)})",
            f"insert arg seq={seq}",
        )
    await _exec(mcp, "COMMIT", "commit")
