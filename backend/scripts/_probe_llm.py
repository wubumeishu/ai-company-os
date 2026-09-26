import os
import time

import httpx

key = os.environ.get("AGNES_API_KEY","").strip()
base = os.environ.get("AGNES_BASE_URL","https://apihub.agnes-ai.com/v1").rstrip("/")
model = os.environ.get("AGNES_MODEL","agnes-3.0-flash")
url = f"{base}/chat/completions"
payload = {"model": model, "messages":[{"role":"user","content":"Reply with the single word OK. No tools, no extra text."}], "max_tokens": 20, "stream": False}
hdrs = {"Authorization": f"Bearer {key}", "Content-Type":"application/json"}
t0 = time.time()
try:
    r = httpx.post(url, json=payload, headers=hdrs, timeout=60)
    print(f"HTTP {r.status_code} in {time.time()-t0:.1f}s")
    print(r.text[:600])
except Exception as e:  # noqa: BLE001 - diagnostic probe: any transport failure
    # (DNS, TLS, timeout, HTTP client error) is itself the answer — report it.
    print(f"EXC {type(e).__name__}: {str(e)[:300]} in {time.time()-t0:.1f}s")
