from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import streamlit as st
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.agent.graph import DEFAULT_DATA_DIR, DEFAULT_OUTPUT_DIR, build_tools
from src.core.llm import build_chat_model, normalize_content
from src.core.prompts import PROMPT
from src.utils.data_store import OrderDataStore


def build_streamlit_agent(*, prompt_key: str, provider: str, model_name: str | None, today: str):
    store = OrderDataStore(DEFAULT_DATA_DIR, DEFAULT_OUTPUT_DIR, today=today)
    model = build_chat_model(provider=provider, model_name=model_name or None, temperature=0.0)
    prompt_template = PROMPT[prompt_key]
    return create_agent(
        model=model,
        tools=build_tools(store),
        system_prompt=prompt_template.format(current_day=today),
    )


def extract_final_answer(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = normalize_content(message.content)
            if text:
                return text
    return ""


def extract_tool_trace(messages: list[Any]) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []

    for message in messages:
        if isinstance(message, AIMessage):
            for call in getattr(message, "tool_calls", []) or []:
                pending[call["id"]] = {
                    "name": call["name"],
                    "args": call.get("args", {}) or {},
                }
        elif isinstance(message, ToolMessage):
            metadata = pending.pop(message.tool_call_id, {})
            trace.append(
                {
                    "name": str(getattr(message, "name", None) or metadata.get("name", "")),
                    "args": metadata.get("args", {}),
                    "output": normalize_content(message.content),
                }
            )

    for metadata in pending.values():
        trace.append({"name": metadata["name"], "args": metadata["args"], "output": ""})
    return trace


def extract_saved_result(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(trace):
        if record["name"] != "save_order" or not record["output"]:
            continue
        try:
            payload = json.loads(record["output"])
        except json.JSONDecodeError:
            continue
        if payload.get("status") == "saved":
            return payload
    return None


def extract_token_usage(messages: list[Any]) -> dict[str, int]:
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for message in messages:
        message_usage = getattr(message, "usage_metadata", None) or {}
        response_usage = getattr(message, "response_metadata", {}).get("token_usage", {})
        for source in (message_usage, response_usage):
            usage["input_tokens"] += int(source.get("input_tokens", source.get("prompt_tokens", 0)) or 0)
            usage["output_tokens"] += int(source.get("output_tokens", source.get("completion_tokens", 0)) or 0)
            usage["total_tokens"] += int(source.get("total_tokens", 0) or 0)

    if usage["total_tokens"] == 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def run_query(*, query: str, prompt_key: str, provider: str, model_name: str | None, today: str) -> dict[str, Any]:
    agent = build_streamlit_agent(prompt_key=prompt_key, provider=provider, model_name=model_name, today=today)
    started_at = time.perf_counter()
    response = agent.invoke({"messages": [{"role": "user", "content": query}]})
    latency = time.perf_counter() - started_at

    messages = response["messages"] if isinstance(response, dict) else response
    trace = extract_tool_trace(messages)
    saved_result = extract_saved_result(trace)
    return {
        "answer": extract_final_answer(messages),
        "trace": trace,
        "saved_result": saved_result,
        "token_usage": extract_token_usage(messages),
        "latency": latency,
        "time_to_first_token": latency,
    }


def render_tool_trace(trace: list[dict[str, Any]]) -> None:
    if not trace:
        st.info("No tool calls.")
        return

    for index, record in enumerate(trace, start=1):
        with st.expander(f"{index}. {record['name']}", expanded=index == 1):
            st.caption("Arguments")
            st.json(record["args"])
            st.caption("Output")
            try:
                st.json(json.loads(record["output"]))
            except (TypeError, json.JSONDecodeError):
                st.code(record["output"] or "(empty)")


def main() -> None:
    st.set_page_config(page_title="OrderDesk Agent", layout="wide")
    st.title("OrderDesk Agent")

    with st.sidebar:
        st.header("Settings")
        prompt_key = st.selectbox("Prompt", options=list(PROMPT), index=0)
        provider = st.selectbox("Provider", options=["google", "ollama"], index=0)
        model_name = st.text_input("Model override", value="")
        today = st.text_input("Today", value="2026-06-01")
        with st.expander("Selected prompt"):
            st.code(PROMPT[prompt_key].format(current_day=today), language="text")

    query = st.text_area(
        "Query input",
        value=(
            "Tạo đơn hàng cho Nguyễn Lan Anh, số điện thoại 0901234567, email lananh@example.com, "
            "giao đến 18 Nguyễn Huệ, Quận 1, TP.HCM. Tôi cần 1 ASUS ROG Zephyrus G14, "
            "2 Logitech Pebble 2 M350s và 1 LG UltraGear 27GP850-B."
        ),
        height=140,
    )

    if st.button("Run agent", type="primary", disabled=not query.strip()):
        with st.spinner("Running agent..."):
            try:
                st.session_state.result = run_query(
                    query=query.strip(),
                    prompt_key=prompt_key,
                    provider=provider,
                    model_name=model_name.strip() or None,
                    today=today.strip() or "2026-06-01",
                )
            except Exception as exc:
                st.session_state.result = {"error": str(exc)}

    result = st.session_state.get("result")
    if not result:
        return

    if "error" in result:
        st.error(result["error"])
        return

    usage = result["token_usage"]
    metric_cols = st.columns(5)
    metric_cols[0].metric("Input tokens", usage["input_tokens"])
    metric_cols[1].metric("Output tokens", usage["output_tokens"])
    metric_cols[2].metric("Total tokens", usage["total_tokens"])
    metric_cols[3].metric("Time to first token", f"{result['time_to_first_token']:.2f}s")
    metric_cols[4].metric("Latency", f"{result['latency']:.2f}s")

    left, right = st.columns([1, 1])
    with left:
        st.subheader("Output")
        st.write(result["answer"] or "(empty)")
        st.subheader("Trace")
        render_tool_trace(result["trace"])

    with right:
        st.subheader("Saved result")
        saved_result = result["saved_result"]
        if saved_result:
            st.json(saved_result)
        else:
            st.info("No saved order.")


if __name__ == "__main__":
    main()
