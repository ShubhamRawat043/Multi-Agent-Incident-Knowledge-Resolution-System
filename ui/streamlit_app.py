"""Streamlit incident console — list, blackboard, approvals, RCA viewer."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

# `streamlit run ui/streamlit_app.py` puts ui/ on sys.path, not the project
# root, so the project packages are not importable without this.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import settings  # noqa: E402

INGESTION = settings.ingestion_url.rstrip("/")


st.set_page_config(page_title="Incident Console", layout="wide")
st.title("Multi-Agent Incident Console")
st.caption(
    f"Ingestion: `{INGESTION}` · DRY_RUN={settings.dry_run} · "
    f"max supervisor rounds={settings.supervisor_max_rounds}"
)


def api_get(path: str) -> dict:
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(f"{INGESTION}{path}")
        resp.raise_for_status()
        return resp.json()


def api_post(path: str, payload: dict) -> dict:
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(f"{INGESTION}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()


with st.sidebar:
    st.header("Create incident")
    service = st.selectbox("Service", ["payments-api", "orders-api"])
    title = st.text_input("Title", "payments-api errors elevated")
    stacktrace = st.text_area(
        "Stacktrace / details",
        "TypeError in payment processing: unexpected NoneType for amount",
    )
    deploy_tag = st.text_input("Deploy tag (optional)", "v1.8")
    if st.button("🚀 Trigger incident", type="primary"):
        try:
            out = api_post(
                "/incidents",
                {
                    "service": service,
                    "title": title,
                    "stacktrace": stacktrace,
                    "deploy_tag": deploy_tag or None,
                    "kind": "error",
                    "source": "manual",
                },
            )
            st.session_state["last_result"] = out
            st.session_state["selected_id"] = out.get("incident_id")
            st.success(f"Created {out.get('incident_id')}")
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("Open incidents")
    try:
        listed = api_get("/incidents").get("incidents") or []
    except Exception as exc:
        listed = []
        st.warning(f"Could not list incidents: {exc}")

    ids = [i["id"] for i in listed]
    selected = st.selectbox(
        "Select",
        options=ids or [st.session_state.get("selected_id") or ""],
        index=0 if ids else 0,
    )
    if selected:
        st.session_state["selected_id"] = selected
    if st.button("🔄 Refresh selected") and st.session_state.get("selected_id"):
        try:
            st.session_state["detail"] = api_get(
                f"/incidents/{st.session_state['selected_id']}"
            )
        except Exception as exc:
            st.error(str(exc))


incident_id = st.session_state.get("selected_id")
detail: dict[str, Any] = st.session_state.get("detail") or st.session_state.get("last_result") or {}

if incident_id and (not detail or detail.get("incident_id") != incident_id):
    try:
        detail = api_get(f"/incidents/{incident_id}")
        st.session_state["detail"] = detail
    except Exception:
        pass

tab_overview, tab_board, tab_plan, tab_approve, tab_rca, tab_audit = st.tabs(
    ["Overview", "Blackboard", "Plan / Actions", "Approvals", "RCA", "Audit"]
)

with tab_overview:
    if not detail:
        st.info("Trigger or select an incident.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Status", detail.get("status") or "—")
        c2.metric("Confidence", f"{float(detail.get('confidence') or 0):.2f}")
        c3.write(f"**ID:** `{detail.get('incident_id')}`")
        c4.write(f"**Next:** `{detail.get('next_action') or detail.get('next')}`")
        st.subheader("Root cause")
        st.json(detail.get("root_cause") or {})
        st.subheader("Verification")
        st.json(detail.get("verification") or {})

with tab_board:
    hypos = detail.get("hypotheses") or []
    if not hypos:
        st.info("No hypotheses yet.")
    else:
        for h in hypos:
            st.markdown(
                f"**{h.get('source_agent')}** — confidence `{h.get('confidence')}`"
            )
            st.write(h.get("statement"))
            if h.get("evidence"):
                st.caption("Evidence: " + "; ".join(map(str, h.get("evidence") or [])))
            st.divider()

with tab_plan:
    st.subheader("Plan")
    st.json(detail.get("plan") or {})
    st.subheader("Executed actions")
    st.json(detail.get("executed_actions") or [])

with tab_approve:
    pending = detail.get("pending_interrupt")
    st.subheader("Pending interrupt")
    if pending:
        st.warning("Graph is waiting for human input")
        st.json(pending)
        decision = st.selectbox("Decision", ["yes", "no", "ack"])
        approver = st.text_input("Approver", "operator")
        role = st.selectbox("Role", settings.t3_approver_roles + ["operator"])
        notes = st.text_input("Notes", "")
        if st.button("Submit decision", type="primary"):
            try:
                out = api_post(
                    "/approve",
                    {
                        "incident_id": detail.get("incident_id") or incident_id,
                        "decision": decision,
                        "approver": approver,
                        "role": role,
                        "notes": notes,
                    },
                )
                st.session_state["detail"] = out
                st.session_state["last_result"] = out
                st.success("Resumed graph")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        st.info("No pending approval / escalation.")

with tab_rca:
    rca = detail.get("rca_report_md") or ""
    if not rca:
        st.info("RCA not generated yet.")
    else:
        st.markdown(rca)
        st.download_button(
            "⬇️ Download RCA",
            data=rca.encode("utf-8"),
            file_name=f"{detail.get('incident_id', 'incident')}_rca.md",
            mime="text/markdown",
        )

with tab_audit:
    st.json(detail.get("audit_log") or [])
