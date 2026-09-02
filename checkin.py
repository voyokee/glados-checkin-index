#!/usr/bin/env python3
"""GLaDOS 每日签到。Cookie 从环境变量或同目录 .env 读取，不要写进仓库。"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "checkin.log"
TZ_CST = timezone(timedelta(hours=8))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)
CHECKIN_OK = {0, 1}  # 0 成功，1 今天已签过


def log(msg: str) -> None:
    line = f"{datetime.now(TZ_CST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def cookie() -> str:
    value = os.environ.get("GLADOS_COOKIE", "").strip()
    if not value:
        sys.exit("缺少 GLADOS_COOKIE。把浏览器 checkin 请求里的 Cookie 写进 .env。")
    return value


def detect_proxy() -> str | None:
    explicit = (
        os.environ.get("GLADOS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    if explicit:
        return explicit.rstrip("/")
    for port in (7897, 7890, 10809, 1087):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return None


def make_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def api(
    opener: urllib.request.OpenerDirector,
    host: str,
    method: str,
    path: str,
    cookie_header: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    url = f"https://{host}{path}"
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("Origin", f"https://{host}")
    req.add_header("User-Agent", UA)
    req.add_header("Cookie", cookie_header)
    with opener.open(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        return resp.status, payload


def with_retry(fn, retries: int = 3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last = RuntimeError(f"HTTP {e.code}: {body[:300]}")
            if e.code in (400, 401, 403, 404):
                raise last from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < retries:
            time.sleep(2 * attempt)
    raise last  # type: ignore[misc]


def summarize_status(payload: dict) -> str:
    data = payload.get("data") or {}
    days = data.get("leftDays")
    if days is not None:
        return f"剩余 {float(days):.0f} 天"
    return "无 leftDays 字段"


def main() -> int:
    load_dotenv()
    host = os.environ.get("GLADOS_HOST", "glados.rocks").strip() or "glados.rocks"
    ck = cookie()
    proxy = detect_proxy()
    opener = make_opener(proxy)
    log(f"开始签到 host={host} proxy={proxy or 'direct'}")

    status, payload = with_retry(
        lambda: api(opener, host, "POST", "/api/user/checkin", ck, {"token": host})
    )
    code = payload.get("code")
    points = payload.get("points")
    message = payload.get("message")
    log(f"checkin http={status} code={code} points={points} message={message}")

    try:
        _, st = api(opener, host, "GET", "/api/user/status", ck)
        log(f"status {summarize_status(st)}")
    except Exception as e:
        log(f"查询 status 失败: {e}")

    try:
        _, pts = api(opener, host, "GET", "/api/user/points", ck)
        total = pts.get("points")
        if total is not None:
            log(f"积分 {int(float(total))}")
    except Exception:
        pass

    if code in CHECKIN_OK:
        return 0
    log("签到失败：接口未返回成功/已签到。Cookie 过期时请重新从浏览器复制。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"异常: {e}")
        raise SystemExit(2)
