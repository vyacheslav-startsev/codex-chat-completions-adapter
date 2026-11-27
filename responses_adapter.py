"""Адаптер из OpenAI Responses API в Chat Completions для Codex CLI.

Codex CLI (0.171+) умеет говорить с моделью только через Responses API
(wire_api = "responses"), а многие провайдеры (например, Cloud.ru Foundation Models
с GigaChat или подписка z.ai GLM Coding Plan) отдают только /v1/chat/completions. Адаптер принимает от Codex POST /v1/responses со stream=true,
собирает из input сообщения Chat Completions, вызывает апстрим в потоковом режиме
и переводит его чанки обратно в события Responses SSE: текст, вызовы инструментов
(function_call) и рассуждения (reasoning).

Запуск:
    python3 responses_adapter.py <port> <upstream_base_url> <ИМЯ_ПЕРЕМЕННОЙ_С_КЛЮЧОМ> [alias=upstream_model ...]

Пример для Cloud.ru:
    export CLOUDRU_API_KEY=...
    python3 responses_adapter.py 8094 https://foundation-models.api.cloud.ru/v1 CLOUDRU_API_KEY

Адрес прослушивания по умолчанию 127.0.0.1, другой задаётся переменной ADAPTER_HOST.
Зависимостей нет, только стандартная библиотека Python 3.9+.
"""
import http.client
import http.server
import json
import os
import sys
import time
import uuid
from urllib.parse import urlsplit

CFG = {}


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "\n".join(parts)


def build_messages(req):
    msgs = []
    if req.get("instructions"):
        msgs.append({"role": "system", "content": req["instructions"]})
    items = req.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    cur = None  # открытое сообщение ассистента
    pending_reasoning = None

    def flush():
        nonlocal cur
        if cur is not None:
            if not cur.get("tool_calls"):
                cur.pop("tool_calls", None)
            if cur.get("content") is None and not cur.get("tool_calls"):
                cur["content"] = ""
            msgs.append(cur)
            cur = None

    def assistant():
        nonlocal cur, pending_reasoning
        if cur is None:
            cur = {"role": "assistant", "content": None, "tool_calls": []}
            if pending_reasoning:
                cur["reasoning_content"] = pending_reasoning
                pending_reasoning = None
        return cur

    for it in items or []:
        t = it.get("type", "message")
        if t == "reasoning":
            s = "\n".join(x.get("text", "") for x in it.get("summary") or [] if isinstance(x, dict))
            pending_reasoning = s or pending_reasoning
        elif t == "message":
            role = it.get("role", "user")
            txt = text_of(it.get("content"))
            if role == "assistant":
                a = assistant()
                a["content"] = (a["content"] + "\n" if a["content"] else "") + txt
            else:
                flush()
                msgs.append({"role": "system" if role in ("developer", "system") else "user", "content": txt})
        elif t == "function_call":
            assistant()["tool_calls"].append({
                "id": it.get("call_id") or it.get("id"),
                "type": "function",
                "function": {"name": it.get("name"), "arguments": it.get("arguments") or "{}"},
            })
        elif t == "function_call_output":
            flush()
            out = it.get("output")
            msgs.append({"role": "tool", "tool_call_id": it.get("call_id"),
                         "content": out if isinstance(out, str) else text_of(out) or json.dumps(out, ensure_ascii=False)})
        # прочие типы (web_search_call, custom_tool_call и т.п.) пропускаем
    flush()
    return msgs


def build_tools(req):
    tools = []
    for t in req.get("tools") or []:
        if t.get("type") == "function":
            tools.append({"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}}}})
    return tools


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.command, self.path))

    def do_GET(self):
        self.send_response(403 if self.headers.get("Upgrade", "").lower() == "websocket" else 404)
        self.end_headers()

    def sse(self, event, data):
        data["type"] = event
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/responses"):
            self.send_response(404); self.end_headers(); return
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        alias = req.get("model", "")
        body = {"model": CFG["aliases"].get(alias, alias), "messages": build_messages(req), "stream": True,
                "stream_options": {"include_usage": True}}
        tools = build_tools(req)
        if tools:
            body["tools"] = tools
            tc = req.get("tool_choice")
            if isinstance(tc, str):
                body["tool_choice"] = tc
        eff = (req.get("reasoning") or {}).get("effort")
        body["thinking"] = {"type": "disabled" if eff == "none" else "enabled"}
        if req.get("max_output_tokens"):
            body["max_tokens"] = req["max_output_tokens"]

        u = CFG["upstream"]
        conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=900)
        conn.request("POST", u.path.rstrip("/") + "/chat/completions", body=json.dumps(body, ensure_ascii=False).encode(),
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + CFG["key"]})
        resp = conn.getresponse()
        if resp.status != 200:
            err = resp.read()
            sys.stderr.write(f"upstream {resp.status}: {err[:300]!r}\n")
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err)
            return

        rid = "resp_" + uuid.uuid4().hex
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        base = {"id": rid, "object": "response", "created_at": int(time.time()), "model": alias, "status": "in_progress", "output": []}
        self.sse("response.created", {"response": base})

        reasoning, text, calls, usage = [], [], {}, {}
        output, msg_item, idx = [], None, 0
        seq_reasoning_done = False

        def emit_reasoning():
            nonlocal seq_reasoning_done, idx
            if seq_reasoning_done:
                return
            seq_reasoning_done = True
            if reasoning:
                item = {"id": "rs_" + uuid.uuid4().hex, "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "".join(reasoning)}]}
                self.sse("response.output_item.added", {"output_index": idx, "item": item})
                self.sse("response.output_item.done", {"output_index": idx, "item": item})
                output.append(item); idx += 1

        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    # z.ai отдаёт рассуждения в reasoning_content, Cloud.ru (GigaChat) в reasoning
                    r_part = d.get("reasoning_content") or d.get("reasoning")
                    if r_part:
                        reasoning.append(r_part)
                        self.wfile.write(b": thinking\n\n"); self.wfile.flush()
                    if d.get("content"):
                        emit_reasoning()
                        if msg_item is None:
                            msg_item = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                                        "status": "in_progress", "content": []}
                            msg_index = idx; idx += 1
                            self.sse("response.output_item.added", {"output_index": msg_index, "item": msg_item})
                        text.append(d["content"])
                        self.sse("response.output_text.delta", {"item_id": msg_item["id"], "output_index": msg_index,
                                                                "content_index": 0, "delta": d["content"]})
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": []})
                        if tc.get("id"):
                            c["id"] = tc["id"]
                        f = tc.get("function") or {}
                        if f.get("name"):
                            c["name"] += f["name"]
                        if f.get("arguments"):
                            c["args"].append(f["arguments"])
            emit_reasoning()
            if msg_item is not None:
                msg_item.update(status="completed", content=[{"type": "output_text", "text": "".join(text), "annotations": []}])
                self.sse("response.output_item.done", {"output_index": msg_index, "item": msg_item})
                output.insert(msg_index, msg_item) if msg_index <= len(output) else output.append(msg_item)
            for k in sorted(calls):
                c = calls[k]
                cid = c["id"] or "call_" + uuid.uuid4().hex
                item = {"id": "fc_" + uuid.uuid4().hex, "type": "function_call", "status": "completed",
                        "call_id": cid, "name": c["name"], "arguments": "".join(c["args"]) or "{}"}
                self.sse("response.output_item.added", {"output_index": idx, "item": item})
                self.sse("response.output_item.done", {"output_index": idx, "item": item})
                output.append(item); idx += 1
            cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
            base.update(status="completed", output=output, usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "input_tokens_details": {"cached_tokens": cached},
                "output_tokens": usage.get("completion_tokens", 0),
                "output_tokens_details": {"reasoning_tokens": ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0},
                "total_tokens": usage.get("total_tokens", 0)})
            self.sse("response.completed", {"response": base})
        except Exception as e:  # обрыв апстрима посреди потока
            sys.stderr.write(f"stream error: {e!r}\n")
            base.update(status="failed", error={"code": "server_error", "message": f"upstream stream error: {e}"})
            try:
                self.sse("response.failed", {"response": base})
            except Exception:
                pass
        finally:
            conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    port, base_url, key_env = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    CFG["upstream"] = urlsplit(base_url)
    CFG["key"] = os.environ.get(key_env, "").strip()
    if not CFG["key"]:
        sys.exit(f"Переменная {key_env} с ключом API пуста")
    CFG["aliases"] = dict(a.split("=", 1) for a in sys.argv[4:])
    host = os.environ.get("ADAPTER_HOST", "127.0.0.1")
    sys.stderr.write(f"Адаптер слушает http://{host}:{port}/v1, апстрим {base_url}\n")
    http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()
