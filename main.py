from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.agent.graph import DEFAULT_DATA_DIR, DEFAULT_OUTPUT_DIR, build_tools
from src.core.llm import build_chat_model, normalize_content
from src.core.prompts import PROMPT
from src.utils.data_store import OrderDataStore


DEFAULT_QUERY = (
    "Tạo đơn hàng cho Nguyễn Lan Anh, số điện thoại 0901234567, email lananh@example.com, "
    "giao đến 18 Nguyễn Huệ, Quận 1, TP.HCM. Tôi cần 1 ASUS ROG Zephyrus G14, "
    "2 Logitech Pebble 2 M350s và 1 LG UltraGear 27GP850-B."
)


def build_web_agent(*, prompt_key: str, provider: str, model_name: str | None, today: str):
    store = OrderDataStore(DEFAULT_DATA_DIR, DEFAULT_OUTPUT_DIR, today=today)
    model = build_chat_model(provider=provider, model_name=model_name or None, temperature=0.0)
    return create_agent(
        model=model,
        tools=build_tools(store),
        system_prompt=PROMPT[prompt_key].format(current_day=today),
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
    if prompt_key not in PROMPT:
        raise ValueError(f"Unknown prompt: {prompt_key}")
    if provider not in {"google", "ollama", "openai", "9router"}:
        raise ValueError("Provider must be 'google', 'ollama', or 'openai'.")

    agent = build_web_agent(prompt_key=prompt_key, provider=provider, model_name=model_name, today=today)
    started_at = time.perf_counter()
    response = agent.invoke({"messages": [{"role": "user", "content": query}]})
    latency = time.perf_counter() - started_at

    messages = response["messages"] if isinstance(response, dict) else response
    trace = extract_tool_trace(messages)
    return {
        "answer": extract_final_answer(messages),
        "trace": trace,
        "saved_result": extract_saved_result(trace),
        "token_usage": extract_token_usage(messages),
        "latency": latency,
        "time_to_first_token": latency,
    }


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html(INDEX_HTML)
            return
        if self.path == "/api/prompts":
            self._send_json(
                {
                    "prompts": [{"key": key, "text": value} for key, value in PROMPT.items()],
                    "default_query": DEFAULT_QUERY,
                }
            )
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        if self.path != "/api/run":
            self.send_error(404, "Not found")
            return

        try:
            payload = self._read_json()
            result = run_query(
                query=str(payload.get("query", "")).strip(),
                prompt_key=str(payload.get("prompt_key", "default")).strip() or "default",
                provider=str(payload.get("provider", "google")).strip() or "google",
                model_name=str(payload.get("model_name", "")).strip() or None,
                today=str(payload.get("today", "2026-06-01")).strip() or "2026-06-01",
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json(result)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {format % args}")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OrderDesk Agent</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #647084;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --bad: #b42318;
      --code: #101828;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 16px 24px;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; letter-spacing: 0; }
    main {
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      gap: 16px;
      padding: 16px;
    }
    aside, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    label { display: block; margin: 12px 0 6px; color: var(--muted); font-size: 13px; }
    select, input, textarea, button {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }
    textarea { min-height: 150px; resize: vertical; }
    button {
      margin-top: 12px;
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font-weight: 700;
    }
    button:hover { background: var(--accent-dark); }
    button:disabled { opacity: .65; cursor: wait; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #f9fafb;
      border-radius: 6px;
      padding: 12px;
      max-height: 360px;
      overflow: auto;
    }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }
    .metrics { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .metric { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fbfcfe; }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong { display: block; font-size: 18px; margin-top: 4px; }
    .answer { min-height: 96px; border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: #fbfcfe; }
    .trace-item { border: 1px solid var(--line); border-radius: 6px; margin-bottom: 10px; overflow: hidden; }
    .trace-title { padding: 10px 12px; background: #eef2f7; font-weight: 700; }
    .trace-body { padding: 12px; display: grid; gap: 10px; }
    .error { color: var(--bad); border: 1px solid #fecdca; background: #fffbfa; padding: 12px; border-radius: 6px; }
    .muted { color: var(--muted); }
    @media (max-width: 900px) {
      main, .grid { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <header><h1>OrderDesk Agent</h1></header>
  <main>
    <aside>
      <h2>Settings</h2>
      <label for="prompt">Prompt</label>
      <select id="prompt"></select>
      <label for="provider">Provider</label>
      <select id="provider">
        <option value="google">google</option>
        <option value="ollama">ollama</option>
        <option value="openai">openai</option>
      </select>
      <label for="model">Model override</label>
      <input id="model" placeholder="optional" />
      <label for="today">Today</label>
      <input id="today" value="2026-06-01" />
      <label>Selected prompt</label>
      <pre id="promptText"></pre>
    </aside>
    <section>
      <h2>Query Input</h2>
      <textarea id="query"></textarea>
      <button id="run">Run agent</button>
      <div id="status" class="muted"></div>
      <div id="error"></div>
      <div class="metrics">
        <div class="metric"><span>Input tokens</span><strong id="inputTokens">0</strong></div>
        <div class="metric"><span>Output tokens</span><strong id="outputTokens">0</strong></div>
        <div class="metric"><span>Total tokens</span><strong id="totalTokens">0</strong></div>
        <div class="metric"><span>First token</span><strong id="firstToken">0.00s</strong></div>
        <div class="metric"><span>Latency</span><strong id="latency">0.00s</strong></div>
      </div>
      <div class="grid">
        <div>
          <h2>Output</h2>
          <div id="answer" class="answer muted">No output yet.</div>
          <h2 style="margin-top:16px">Trace</h2>
          <div id="trace" class="muted">No tool calls.</div>
        </div>
        <div>
          <h2>Saved Result</h2>
          <pre id="saved">No saved order.</pre>
        </div>
      </div>
    </section>
  </main>
  <script>
    const state = { prompts: [] };
    const $ = (id) => document.getElementById(id);

    function pretty(value) {
      if (typeof value === "string") {
        try { return JSON.stringify(JSON.parse(value), null, 2); }
        catch { return value; }
      }
      return JSON.stringify(value, null, 2);
    }

    function selectedPromptText() {
      const key = $("prompt").value;
      const item = state.prompts.find((prompt) => prompt.key === key);
      return item ? item.text.replaceAll("{current_day}", $("today").value || "2026-06-01") : "";
    }

    function renderPrompt() {
      $("promptText").textContent = selectedPromptText();
    }

    function renderTrace(trace) {
      if (!trace || trace.length === 0) {
        $("trace").className = "muted";
        $("trace").textContent = "No tool calls.";
        return;
      }
      $("trace").className = "";
      $("trace").innerHTML = trace.map((item, index) => `
        <div class="trace-item">
          <div class="trace-title">${index + 1}. ${item.name || "(unknown)"}</div>
          <div class="trace-body">
            <div><strong>Arguments</strong><pre>${escapeHtml(pretty(item.args || {}))}</pre></div>
            <div><strong>Output</strong><pre>${escapeHtml(pretty(item.output || ""))}</pre></div>
          </div>
        </div>
      `).join("");
    }

    function escapeHtml(text) {
      return String(text)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    async function loadPrompts() {
      const response = await fetch("/api/prompts");
      const payload = await response.json();
      state.prompts = payload.prompts;
      $("prompt").innerHTML = state.prompts.map((item) => `<option value="${item.key}">${item.key}</option>`).join("");
      $("query").value = payload.default_query;
      renderPrompt();
    }

    async function runAgent() {
      $("run").disabled = true;
      $("status").textContent = "Running agent...";
      $("error").innerHTML = "";
      try {
        const response = await fetch("/api/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: $("query").value,
            prompt_key: $("prompt").value,
            provider: $("provider").value,
            model_name: $("model").value,
            today: $("today").value
          })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Request failed");
        const usage = payload.token_usage || {};
        $("inputTokens").textContent = usage.input_tokens || 0;
        $("outputTokens").textContent = usage.output_tokens || 0;
        $("totalTokens").textContent = usage.total_tokens || 0;
        $("firstToken").textContent = `${Number(payload.time_to_first_token || 0).toFixed(2)}s`;
        $("latency").textContent = `${Number(payload.latency || 0).toFixed(2)}s`;
        $("answer").className = "answer";
        $("answer").textContent = payload.answer || "(empty)";
        renderTrace(payload.trace || []);
        $("saved").textContent = payload.saved_result ? pretty(payload.saved_result) : "No saved order.";
        $("status").textContent = "Done.";
      } catch (error) {
        $("error").innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
        $("status").textContent = "";
      } finally {
        $("run").disabled = false;
      }
    }

    $("prompt").addEventListener("change", renderPrompt);
    $("today").addEventListener("input", renderPrompt);
    $("run").addEventListener("click", runAgent);
    loadPrompts().catch((error) => {
      $("error").innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
    });
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the dependency-free OrderDesk web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"OrderDesk web UI running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
