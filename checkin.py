#!/usr/bin/env python3
"""GLaDOS 按 IQI 阈值签到。Cookie 从环境变量或同目录 .env 读取，不要写进仓库。"""

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
from typing import Literal

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
CHECKIN_OK = {0, 1}
DOMAINS = ("glados.cloud", "glados.rocks", "glados.network")

# (北京时间整点起, 严格小于该值才签到)
IQI_THRESHOLDS = (
    (14, 75),
    (16, 85),
    (18, 95),
)
WINDOW_START_MINUTES = 14 * 60
FORCE_CHECKIN_MINUTES = 20 * 60  # 20:00 起无条件兜底
WINDOW_END_MINUTES = 22 * 60

ForceCheckin = Literal["FORCE"]
ThresholdResult = int | ForceCheckin | None


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


def beijing_now() -> datetime:
    return datetime.now(TZ_CST)


def minutes_of_day(now: datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    local = now.astimezone(TZ_CST)
    return local.hour * 60 + local.minute


def get_iqi_threshold(now: datetime) -> ThresholdResult:
    """返回当前北京时间对应的 IQI 阈值、FORCE（20:00 兜底）或 None（窗口外）。"""
    minutes = minutes_of_day(now)
    if minutes < WINDOW_START_MINUTES or minutes > WINDOW_END_MINUTES:
        return None
    if minutes >= FORCE_CHECKIN_MINUTES:
        return "FORCE"
    threshold: int | None = None
    hour = now.astimezone(TZ_CST).hour
    for start_hour, value in IQI_THRESHOLDS:
        if hour >= start_hour:
            threshold = value
    return threshold


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


def domain_list() -> list[str]:
    override = os.environ.get("GLADOS_HOST", "").strip()
    hosts = list(DOMAINS)
    if override:
        hosts = [override] + [h for h in hosts if h != override]
    return hosts


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
    req.add_header("Referer", f"https://{host}/console/checkin")
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


def api_with_domains(
    opener: urllib.request.OpenerDirector,
    method: str,
    path: str,
    cookie_header: str,
    body: dict | None = None,
) -> tuple[str, int, dict]:
    errors: list[str] = []
    for host in domain_list():
        try:
            status, payload = with_retry(
                lambda h=host: api(opener, h, method, path, cookie_header, body)
            )
            return host, status, payload
        except Exception as e:
            errors.append(f"{host}: {e}")
    raise RuntimeError("所有域名均失败: " + "; ".join(errors))


def parse_iqi(payload: dict) -> dict:
    if payload.get("code") != 0:
        raise ValueError(f"code={payload.get('code')}")
    quality = (payload.get("data") or {}).get("quality")
    if not isinstance(quality, dict) or "score" not in quality:
        raise ValueError("缺少 data.quality.score")
    score = quality["score"]
    if not isinstance(score, (int, float)):
        raise ValueError("score 不是数值")
    score_f = float(score)
    if not 0 <= score_f <= 100:
        raise ValueError(f"score 超出 0-100: {score_f}")
    return {
        "score": score_f,
        "level": quality.get("level", ""),
        "bandwidth": quality.get("bandwidth"),
        "latency": quality.get("latency"),
    }


def get_iqi(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
) -> tuple[str, dict]:
    for host in domain_list():
        try:
            status, payload = with_retry(
                lambda h=host: api(opener, h, "GET", "/api/user/iqi", cookie_header)
            )
            if status != 200:
                raise ValueError(f"HTTP {status}")
            quality = parse_iqi(payload)
            return host, quality
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"IQI 查询失败: {last}")


def already_checked_in_today(points_payload: dict, now: datetime) -> bool:
    if points_payload.get("code") != 0:
        return False
    today = now.astimezone(TZ_CST).strftime("%Y-%m-%d")
    for item in points_payload.get("history") or []:
        if not isinstance(item, dict):
            continue
        ts = str(
            item.get("time")
            or item.get("date")
            or item.get("createdAt")
            or item.get("created_at")
            or ""
        )
        if today not in ts:
            continue
        change = item.get("change", item.get("points"))
        try:
            if change is not None and float(change) > 0:
                return True
        except (TypeError, ValueError):
            pass
        reason = str(item.get("reason", item.get("type", ""))).lower()
        if any(k in reason for k in ("check", "sign", "daily", "observation")):
            return True
    return False


def summarize_status(payload: dict) -> str:
    data = payload.get("data") or {}
    days = data.get("leftDays")
    if days is not None:
        return f"剩余 {float(days):.0f} 天"
    return "无 leftDays 字段"


def do_checkin(opener: urllib.request.OpenerDirector, ck: str) -> int:
    errors: list[str] = []
    for host in domain_list():
        try:
            status, payload = with_retry(
                lambda h=host: api(
                    opener, h, "POST", "/api/user/checkin", ck, {"token": h}
                )
            )
            code = payload.get("code")
            points = payload.get("points")
            message = payload.get("message")
            log(
                f"checkin host={host} http={status} code={code} "
                f"points={points} message={message}"
            )

            try:
                _, st = api_with_domains(opener, "GET", "/api/user/status", ck)
                log(f"status {summarize_status(st)}")
            except Exception as e:
                log(f"查询 status 失败: {e}")

            try:
                _, pts = api_with_domains(opener, "GET", "/api/user/points", ck)
                total = pts.get("points")
                if total is not None:
                    log(f"积分 {int(float(total))}")
            except Exception:
                pass

            if code in CHECKIN_OK:
                return 0
            log("签到失败：接口未返回成功/已签到。Cookie 过期时请重新从浏览器复制。")
            return 1
        except Exception as e:
            errors.append(f"{host}: {e}")
    log("签到失败：所有域名均不可用: " + "; ".join(errors))
    return 1


def main() -> int:
    load_dotenv()
    now = beijing_now()
    ck = cookie()
    proxy = detect_proxy()
    opener = make_opener(proxy)
    log(f"运行检查 北京时间={now:%H:%M} proxy={proxy or 'direct'}")

    threshold = get_iqi_threshold(now)
    if threshold is None:
        log("不在 14:00–22:00 签到窗口，退出")
        return 0

    try:
        _, _, points_payload = api_with_domains(opener, "GET", "/api/user/points", ck)
        if already_checked_in_today(points_payload, now):
            log("今日已签到，退出")
            return 0
    except Exception as e:
        log(f"查询今日签到状态失败: {e}")
        return 2

    if threshold == "FORCE":
        log("到达 20:00 兜底时间，无条件签到")
        return do_checkin(opener, ck)

    try:
        host, iqi = get_iqi(opener, ck)
        score = iqi["score"]
        log(
            f"IQI host={host} score={score:g} level={iqi['level']} "
            f"bandwidth={iqi['bandwidth']} latency={iqi['latency']}"
        )
    except Exception as e:
        log(f"IQI 查询失败: {e}，等待下次运行")
        return 0

    if score >= threshold:
        log(f"Index {score:g} >= 阈值 {threshold}（严格小于才签），继续等待")
        return 0

    log(f"Index {score:g} < 阈值 {threshold}，执行签到")
    return do_checkin(opener, ck)


def _test_thresholds() -> None:
    cases = [
        ("13:59", None),
        ("14:00", 75),
        ("15:59", 75),
        ("16:00", 85),
        ("17:59", 85),
        ("18:00", 95),
        ("19:59", 95),
        ("20:00", "FORCE"),
        ("21:30", "FORCE"),
        ("22:00", "FORCE"),
        ("22:01", None),
    ]
    for hm, expected in cases:
        h, m = map(int, hm.split(":"))
        dt = datetime(2026, 1, 1, h, m, tzinfo=TZ_CST)
        got = get_iqi_threshold(dt)
        assert got == expected, f"{hm}: got {got!r}, want {expected!r}"
    print("threshold tests ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-threshold":
        _test_thresholds()
        raise SystemExit(0)
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"异常: {e}")
        raise SystemExit(2)
