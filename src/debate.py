"""
Orchestrate an adversarial multi-agent DBA debate over a database incident.

    Mitigator (restore service fast) ─┐
    Guardian  (risk / root cause)    ─┤── read-only SQL + incident search ─> Oracle 26ai
    Incident Commander (Judge)       ─┘

Each agent runs a real OpenAI function-calling loop, investigating the captured
incident snapshot (read-only) and the past-incident knowledge base, then argues its
case. The Commander synthesises a remediation runbook. Agents never change the DB —
they recommend; a human executes.

Run:  python src/debate.py
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from typing import Any

from openai import AsyncOpenAI

import config
import persist
from mcp_oracle import open_oracle_mcp, OracleMCP
from tools import TOOL_SPECS, dispatch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

MAX_TOOL_TURNS = 8


def _wrap(text: str, indent: str = "   ") -> str:
    out = []
    for para in text.splitlines():
        out.append(textwrap.fill(para, width=92, initial_indent=indent,
                                 subsequent_indent=indent) if para.strip() else "")
    return "\n".join(out)


async def run_agent(*, client: AsyncOpenAI, mcp: OracleMCP, system_prompt: str,
                    user_prompt: str, label: str) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    for _ in range(MAX_TOOL_TURNS):
        resp = await client.chat.completions.create(
            model=config.OPENAI_MODEL, messages=messages, tools=TOOL_SPECS, temperature=0.3,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return (msg.content or "").strip()
        messages.append({
            "role": "assistant", "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            import json
            args = json.loads(tc.function.arguments or "{}")
            preview = args.get("sql") or args.get("query_text") or ""
            print(f"      [{label} → {tc.function.name}] {preview[:90]}")
            try:
                result = await dispatch(tc.function.name, args, mcp=mcp, openai_client=client)
            except Exception as exc:
                result = f"ERROR: {exc}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:6000]})
    return "(agent exhausted its tool budget without concluding)"


READONLY = (
    " You may ONLY run read-only SELECT queries via run_sql. You must NOT execute any "
    "change (KILL SESSION, ALTER, DML, etc.) — instead, write out the exact command(s) you "
    "recommend a human DBA run, with the reason. Cite specific figures (SIDs, events, counts) "
    "you retrieved."
)

MITIGATOR_SYSTEM = (
    "You are the on-call DBA in an incident bridge, focused on RESTORING SERVICE FAST. "
    "Investigate the incident snapshot, find what is hurting users right now, and argue for "
    "the quickest action that safely restores service. Be decisive." + READONLY
)
GUARDIAN_SYSTEM = (
    "You are the senior DBA focused on SAFETY and ROOT CAUSE. Challenge fast fixes: spell out "
    "blast radius, data-loss / rollback risk, and prerequisites. You MUST call incident_search "
    "with the current symptoms and explicitly cite the closest past incident (its title and "
    "distance) and how it was safely resolved — use that precedent in your argument. Insist on "
    "confirming the root cause before any destructive action, and propose safer alternatives or "
    "a safe ordering." + READONLY
)
COMMANDER_SYSTEM = (
    "You are the Incident Commander. Two DBAs have argued — one for fast mitigation, one for "
    "caution and root cause. Produce a concise remediation RUNBOOK with these sections: "
    "1) Severity & impact, 2) Root-cause hypothesis (cite evidence), 3) Ordered remediation steps "
    "— mark each step [SAFE/READ-ONLY] or [CHANGE — needs approval] and give the exact command, "
    "4) Rollback plan, 5) What to monitor to confirm recovery. Be specific and practical."
)


async def main() -> None:
    iid = config.INCIDENT_ID
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY or None)
    print(f"\n=== DBA incident debate — Incident {iid} ===\n")

    async with open_oracle_mcp(config.SQLCL_COMMAND, config.ORACLE_MCP_CONNECTION) as mcp:
        print(f"Connected to Oracle via SQLcl MCP. Tools: {', '.join(mcp.tool_names)}\n")
        case = (
            f"Investigate incident_id = {iid}. Read its row in `incidents`, the `incident_metrics`, "
            f"the `session_snapshot`, and `sql_snapshot` (all filtered by incident_id = {iid}), then "
            "make your case."
        )

        print("🚑 Mitigator is investigating...")
        mit = await run_agent(client=client, mcp=mcp, system_prompt=MITIGATOR_SYSTEM,
                              user_prompt=case, label="MITIGATOR")
        print("\n🚑 MITIGATOR:\n" + _wrap(mit) + "\n")

        print("🛡️  Guardian is challenging the fast fix...")
        guard = await run_agent(client=client, mcp=mcp, system_prompt=GUARDIAN_SYSTEM,
                                user_prompt=case + "\n\nThe on-call DBA will push for a fast fix; "
                                "scrutinise the risk and find the root cause.", label="GUARDIAN")
        print("\n🛡️  GUARDIAN:\n" + _wrap(guard) + "\n")

        print("🔁 Mitigator responds to Guardian's concerns...")
        mit2 = await run_agent(client=client, mcp=mcp, system_prompt=MITIGATOR_SYSTEM,
                               user_prompt=f"{case}\n\nThe senior DBA raised these concerns:\n\n{guard}\n\n"
                               "Respond: concede what is valid, and refine your recommendation into a "
                               "safe-but-fast plan.", label="MITIGATOR-2")
        print("\n🔁 MITIGATOR (response):\n" + _wrap(mit2) + "\n")

        print("🧭 Incident Commander is writing the runbook...")
        verdict = await client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": COMMANDER_SYSTEM},
                {"role": "user", "content": (
                    f"Incident {iid}.\n\n=== Mitigator (fast) ===\n{mit}\n\n"
                    f"=== Guardian (safety/root cause) ===\n{guard}\n\n"
                    f"=== Mitigator response ===\n{mit2}\n\nWrite the runbook.")},
            ],
            temperature=0.2,
        )
        runbook = verdict.choices[0].message.content or ""
        print("\n🧭 RUNBOOK:\n" + _wrap(runbook) + "\n")

        await persist.ensure_tables(mcp)
        await persist.save_debate(
            mcp, run_id=int(time.time()), incident_id=iid, model=config.OPENAI_MODEL,
            arguments=[(1, "opening", "MITIGATOR", mit), (2, "opening", "GUARDIAN", guard),
                       (3, "response", "MITIGATOR", mit2)],
            runbook=runbook,
        )
        print("💾 Saved to dba_debate_runs / dba_debate_arguments (view: v_dba_debate_feed).")


if __name__ == "__main__":
    asyncio.run(main())
