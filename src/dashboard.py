"""
Streamlit dashboard for the DBA incident debate, reading from Oracle 26ai through
the SQLcl MCP server.

Run from the project root:
    streamlit run src/dashboard.py
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

import config
import dashboard_data as dd
from mcp_oracle import open_oracle_mcp

PERSONA_STYLE = {
    "MITIGATOR": ("🚑 MITIGATOR · restore service fast", "#1c1416", "#f87171"),
    "GUARDIAN": ("🛡 GUARDIAN · safety & root cause", "#0f1825", "#60a5fa"),
}


def _load_all_sync() -> list[dict]:
    async def _go() -> list[dict]:
        async with open_oracle_mcp(config.SQLCL_COMMAND, config.ORACLE_MCP_CONNECTION) as mcp:
            runs = await dd.list_runs(mcp)
            out: list[dict] = []
            for r in runs:
                detail = await dd.get_run(mcp, r["run_id"])
                if detail:
                    out.append(detail)
            return out

    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(_go())).result()


@st.cache_data(show_spinner="Loading incident debates from Oracle 26ai via SQLcl MCP…")
def load_all(nonce: int) -> list[dict]:
    return _load_all_sync()


def argument_card(persona: str, phase: str, content: str) -> None:
    title, bg, fg = PERSONA_STYLE.get(persona, (persona, "#eee", "#333"))
    st.markdown(
        f"<div style='background:{bg};border-left:5px solid {fg};padding:10px 14px;"
        f"border-radius:6px;margin-bottom:6px;'><b style='color:{fg};'>{title} · {phase}</b></div>",
        unsafe_allow_html=True,
    )
    st.markdown(content)


def main() -> None:
    st.set_page_config(page_title="DBA Incident War-Room · Oracle 26ai", page_icon="🧭", layout="wide")
    st.title("🧭 DBA Incident War-Room")
    st.caption("Adversarial multi-agent DBA debates over Oracle 26ai · data via the SQLcl MCP server · agents are read-only advisors")

    with st.sidebar:
        st.header("Controls")
        nonce = st.session_state.setdefault("nonce", 0)
        if st.button("🔄 Refresh from database"):
            st.session_state["nonce"] = nonce + 1
            st.cache_data.clear()
            st.rerun()

    runs = load_all(st.session_state["nonce"])
    if not runs:
        st.warning("No debates yet. Run `python src/debate.py` to generate one.")
        return

    with st.sidebar:
        options = {
            f"#{r['run_id']} · incident {r['incident_id']} · {r.get('created_at','')}": r
            for r in runs
        }
        choice = st.selectbox("Debate run", list(options.keys()))
    run = options[choice]

    c1, c2, c3 = st.columns(3)
    c1.metric("Incident", run.get("incident_id"))
    c2.metric("Run", run.get("created_at", "—"))
    c3.metric("Model", run.get("model", "—"))
    if run.get("incident_title"):
        st.info(f"**{run['incident_title']}**")

    st.subheader("🧭 Incident Commander — runbook")
    st.success(run.get("runbook") or "—")

    st.subheader("The debate")
    args = sorted(run.get("arguments") or [], key=lambda a: a.get("seq", 0))
    openings = [a for a in args if a.get("phase") == "opening"]
    later = [a for a in args if a.get("phase") != "opening"]

    cols = st.columns(2)
    for col, persona in zip(cols, ("MITIGATOR", "GUARDIAN")):
        with col:
            for a in openings:
                if a["persona"] == persona:
                    argument_card(a["persona"], a["phase"], a["content"])
    for a in later:
        argument_card(a["persona"], a["phase"], a["content"])


if __name__ == "__main__":
    main()
else:
    main()
