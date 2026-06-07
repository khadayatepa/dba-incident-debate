"""
Read persisted DBA debates back out of Oracle through the SQLcl MCP server.

Oracle aggregates each result into a single JSON value; SQLcl returns it as one
CSV field which Python's csv module unescapes cleanly.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from mcp_oracle import OracleMCP

RUNS_SQL = """
SELECT JSON_ARRAYAGG(
         JSON_OBJECT(
           'run_id'         VALUE r.run_id,
           'incident_id'    VALUE r.incident_id,
           'incident_title' VALUE i.title,
           'created_at'     VALUE TO_CHAR(r.created_at,'YYYY-MM-DD HH24:MI'),
           'model'          VALUE r.model
           RETURNING CLOB)
         ORDER BY r.created_at DESC RETURNING CLOB) AS data
FROM dba_debate_runs r LEFT JOIN incidents i ON i.incident_id = r.incident_id
""".strip()


def _detail_sql(run_id: int) -> str:
    return f"""
SELECT JSON_OBJECT(
         'run_id'         VALUE r.run_id,
         'incident_id'    VALUE r.incident_id,
         'incident_title' VALUE i.title,
         'created_at'     VALUE TO_CHAR(r.created_at,'YYYY-MM-DD HH24:MI'),
         'model'          VALUE r.model,
         'runbook'        VALUE r.runbook,
         'arguments'      VALUE (
            SELECT JSON_ARRAYAGG(
                     JSON_OBJECT('seq' VALUE seq, 'phase' VALUE phase,
                                 'persona' VALUE persona, 'content' VALUE content
                                 RETURNING CLOB)
                     ORDER BY seq RETURNING CLOB)
            FROM dba_debate_arguments a WHERE a.run_id = r.run_id)
         RETURNING CLOB)
       AS data
FROM dba_debate_runs r LEFT JOIN incidents i ON i.incident_id = r.incident_id
WHERE r.run_id = {int(run_id)}
""".strip()


def _extract(out: str) -> Any:
    rows = list(csv.reader(io.StringIO(out)))
    for row in rows[1:]:
        for cell in row:
            cell = cell.strip()
            if cell and cell[0] in "[{":
                return json.loads(cell)
    return None


async def list_runs(mcp: OracleMCP) -> list[dict]:
    return _extract(await mcp.run_sql(RUNS_SQL)) or []


async def get_run(mcp: OracleMCP, run_id: int) -> dict | None:
    return _extract(await mcp.run_sql(_detail_sql(run_id)))
