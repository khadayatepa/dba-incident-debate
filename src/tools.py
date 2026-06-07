"""
The two tools the DBA agents may use — both READ-ONLY.

The agents investigate an incident; they never change anything. `run_sql` rejects
anything that isn't a SELECT/WITH, so an agent cannot KILL a session, ALTER the
database, or run DML even if it tries. Remediation is *recommended* in prose for a
human DBA to execute.
"""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

import config
from mcp_oracle import OracleMCP

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run a READ-ONLY SQL query against the incident's captured diagnostic "
                "snapshot. Tables: "
                "incidents(incident_id, title, severity, opened_at, symptom_text); "
                "incident_metrics(incident_id, metric_name, metric_value, normal_range, note); "
                "session_snapshot(incident_id, sid, serial#, username, status, sql_id, event, "
                "blocking_session, secs_in_wait, module); "
                "sql_snapshot(incident_id, sql_id, sql_text, executions, elapsed_sec_per_exec, "
                "plan_hash, note). Only SELECT statements are allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."}
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "incident_search",
            "description": (
                "Semantic search over the past-incident knowledge base. Embeds your query "
                "(e.g. the current symptoms) and returns the most similar historical "
                "incidents with their category, distance, and how they were resolved. Use it "
                "to ground your recommendation in what worked (or went wrong) before."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {"type": "string", "description": "Symptoms to match against."},
                    "k": {"type": "integer", "description": "How many results (1-10).", "default": 4},
                },
                "required": ["query_text"],
            },
        },
    },
]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _is_readonly(sql: str) -> bool:
    s = sql.strip()
    while s.startswith("("):
        s = s[1:].strip()
    head = s.upper()
    return head.startswith("SELECT") or head.startswith("WITH")


async def _embed(client: AsyncOpenAI, text: str) -> list[float]:
    resp = await client.embeddings.create(model=config.EMBED_MODEL, input=text)
    return resp.data[0].embedding


async def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    mcp: OracleMCP,
    openai_client: AsyncOpenAI,
) -> str:
    if name == "run_sql":
        sql = args["sql"]
        if not _is_readonly(sql):
            return (
                "REFUSED: only read-only SELECT queries are permitted. To change the "
                "database (KILL SESSION, ALTER, DML, etc.), recommend the command for a "
                "human DBA to run — do not execute it."
            )
        return await mcp.run_sql(sql)

    if name == "incident_search":
        k = max(1, min(int(args.get("k", 4) or 4), 10))
        vec = await _embed(openai_client, args["query_text"])
        sql = (
            "SELECT title, category, "
            f"ROUND(VECTOR_DISTANCE(embedding, TO_VECTOR('{_vector_literal(vec)}'), COSINE), 4) "
            "AS distance, symptom_text, resolution_text "
            "FROM past_incidents "
            f"ORDER BY VECTOR_DISTANCE(embedding, TO_VECTOR('{_vector_literal(vec)}'), COSINE) "
            f"FETCH APPROX FIRST {k} ROWS ONLY"
        )
        return await mcp.run_sql(sql)

    return json.dumps({"error": f"unknown tool {name}"})
