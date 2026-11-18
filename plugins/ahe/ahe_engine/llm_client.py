"""极简的 OpenAI 兼容 chat-completions 客户端。"""
from __future__ import annotations

import json
import os
import time
import urllib.request

def call_llm(url, key, model, messages, **kw):
    body = json.dumps({"model": model, "messages": messages, **kw}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=float(os.environ.get('LLM_CALL_TIMEOUT_S', '1500'))) as r:
        d = json.loads(r.read())
    return (d["choices"][0]["message"]["content"],
            d.get("usage", {}).get("total_tokens", 0),
            time.monotonic() - t0)

