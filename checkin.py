#!/usr/bin/env python3
"""GLaDOS v0.2：IQI 四档签到，提交 challenge/nonce/proof。Cookie 只从环境变量或 .env 读取。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
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

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)
CHECKIN_OK = {0, 1}
DOMAINS = ("glados.cloud", "glados.rocks", "glados.network")
CHECKIN_CHALLENGE_DELAY = 2.7
PROOF_SUFFIX = ".glados-observe-v1"
MAX_NONCE_LEN = 4096
CHECKIN_ATTEMPTS = 3
LOW_REWARD_MAX = 1.0
CURRENT_SESSION_COOKIES = ("gld:sess", "gld:sess.sig")
LEGACY_SESSION_COOKIES = ("koa:sess", "koa:sess.sig")
_COOKIE_PAIR_RE = re.compile(r"(?:gld|koa):sess(?:\.sig)?=[^;\s]+", re.IGNORECASE)
_GOT_POINTS_RE = re.compile(r"got\s+(\d+)\s+point", re.IGNORECASE)
_secrets: list[str] = []

# 每天 4 档（对应计划任务 16/17/18/19 点），Index 严格小于该值才签；最后一档 anyscore
CHECKIN_SCHEDULE = (
    (16, 80),
    (17, 85),
    (18, 90),
    (19, None),
)
WINDOW_START_MINUTES = 16 * 60
WINDOW_END_MINUTES = 19 * 60 + 59

AnyScore = Literal["ANY"]
ThresholdResult = int | AnyScore | None


class ConfigError(Exception):
    """本地配置错误。消息不得包含 Cookie 或 User-Agent 原文。"""


class UserAgentConfigurationError(ConfigError):
    pass


class ApiError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.http_status = http_status


def schedule_label(threshold: ThresholdResult) -> str:
    if threshold == "ANY":
        return "19:00 anyscore"
    if threshold == 80:
        return "16:00 Index<80 (+8档)"
    if threshold == 85:
        return "17:00 Index<85"
    if threshold == 90:
        return "18:00 Index<90"
    return str(threshold)


def register_secret(value: str | None) -> None:
    if not value or len(value) < 8:
        return
    if value not in _secrets:
        _secrets.append(value)


def reset_secrets() -> None:
    _secrets.clear()


def redact(msg: str) -> str:
    redacted = _COOKIE_PAIR_RE.sub("[redacted]", msg)
    for secret in _secrets:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    if len(redacted) > 300:
        redacted = redacted[:300] + "…"
    return redacted


def log_path() -> Path:
    override = os.environ.get("GLADOS_LOG_PATH", "").strip()
    return Path(override) if override else LOG_PATH


def log(msg: str) -> None:
    line = f"{datetime.now(TZ_CST):%Y-%m-%d %H:%M:%S} {redact(msg)}"
    print(line, flush=True)
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


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


def cookie_names(cookie_header: str) -> set[str]:
    names: set[str] = set()
    for item in cookie_header.split(";"):
        name, separator, _value = item.strip().partition("=")
        if separator and name:
            names.add(name)
    return names


def session_cookie_kind(cookie_header: str) -> str | None:
    names = cookie_names(cookie_header)
    if set(CURRENT_SESSION_COOKIES).issubset(names):
        return "gld"
    if set(LEGACY_SESSION_COOKIES).issubset(names):
        return "koa"
    return None


def load_cookie() -> tuple[str, str]:
    value = os.environ.get("GLADOS_COOKIE", "").strip()
    if not value:
        raise ConfigError("缺少 GLADOS_COOKIE。把浏览器 checkin 请求里的 Cookie 写进 .env。")
    kind = session_cookie_kind(value)
    if kind is None:
        raise ConfigError(
            "GLADOS_COOKIE 需要成对的 gld:sess 与 gld:sess.sig，或成对的 koa:sess 与 koa:sess.sig。"
        )
    register_secret(value)
    return value, kind


def normalize_user_agent(raw: str) -> tuple[str, bool]:
    """去掉复制进来的引号和请求头名，不改浏览器原文。非法值不回显。"""
    if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in raw):
        raise UserAgentConfigurationError("含有无效控制字符，请重新复制浏览器的 User-Agent")
    value = raw.strip()
    if not value:
        return "", False
    if "\r" in value or "\n" in value:
        raise UserAgentConfigurationError("含有换行，请粘贴完整单行 User-Agent")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise UserAgentConfigurationError(
            "含有不支持的字符，请重新复制浏览器的 User-Agent，不要加入说明文字或中文引号"
        ) from None

    original = value
    prefix_removed = False
    if value.lower().startswith("user-agent:"):
        value = value[len("user-agent:") :].strip()
        prefix_removed = True
    if len(value) >= 2 and value[0] == value[-1]:
        inner = value[1:-1]
        single_quote_wrapper = value[0] == "'" and (
            not inner or any(char in " \t" for char in inner) or inner.lower().startswith("user-agent:")
        )
        if value[0] == '"' or single_quote_wrapper:
            value = inner.strip()
    if not prefix_removed and value.lower().startswith("user-agent:"):
        value = value[len("user-agent:") :].strip()
    if not value:
        raise UserAgentConfigurationError("只有请求头名称或引号，没有 User-Agent 值")
    if value[0] == '"' or value[-1] == '"':
        raise UserAgentConfigurationError("外层双引号不成对或重复，请只保留 User-Agent 值")
    if len(value) >= 2 and value[0] == value[-1] == "'" and any(char in " \t" for char in value):
        raise UserAgentConfigurationError("包裹 User-Agent 的单引号重复，请只保留 User-Agent 值")
    if value.lower().startswith("user-agent:"):
        raise UserAgentConfigurationError("请求头名称重复，请只保留 User-Agent 值")
    return value, value != original


def resolve_user_agent() -> str:
    value, _normalized = normalize_user_agent(os.environ.get("GLADOS_USER_AGENT", ""))
    return value or DEFAULT_USER_AGENT


def describe_user_agent() -> str:
    value, normalized = normalize_user_agent(os.environ.get("GLADOS_USER_AGENT", ""))
    if not value:
        return "未配置，使用默认值"
    status = "已配置"
    if normalized:
        status += "（已自动去除外层引号或请求头名称）"
    return status


def beijing_now() -> datetime:
    return datetime.now(TZ_CST)


def minutes_of_day(now: datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    local = now.astimezone(TZ_CST)
    return local.hour * 60 + local.minute


def get_iqi_threshold(now: datetime) -> ThresholdResult:
    """按当前北京时间所在档位返回 Index 上界或 ANY（19:00 任意 Index 签到）。"""
    minutes = minutes_of_day(now)
    if minutes < WINDOW_START_MINUTES or minutes > WINDOW_END_MINUTES:
        return None
    hour = now.astimezone(TZ_CST).hour
    result: ThresholdResult = None
    for start_hour, bound in CHECKIN_SCHEDULE:
        if hour >= start_hour:
            result = "ANY" if bound is None else bound
    return result


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
    user_agent: str | None = None,
) -> tuple[int, dict]:
    url = f"https://{host}{path}"
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("Origin", f"https://{host}")
    req.add_header("Referer", f"https://{host}/console/checkin")
    req.add_header("User-Agent", user_agent or resolve_user_agent())
    req.add_header("Cookie", cookie_header)
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ApiError("invalid-response", retryable=True)
            return resp.status, payload
    except ApiError:
        raise
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        if e.code in (401, 403):
            raise ApiError("http-auth-rejected", retryable=False, http_status=e.code) from None
        if e.code in (400, 404):
            raise ApiError("http-rejected", retryable=False, http_status=e.code) from None
        raise ApiError("http-transient", retryable=True, http_status=e.code) from None
    except json.JSONDecodeError:
        raise ApiError("invalid-response", retryable=True) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError("network", retryable=True) from None


def with_retry(fn, retries: int = 3):
    last: ApiError | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except ApiError as e:
            if not e.retryable:
                raise
            last = e
        if attempt < retries:
            time.sleep(2 * attempt)
    if last is None:
        raise ApiError("network", retryable=True)
    raise last


def api_with_domains(
    opener: urllib.request.OpenerDirector,
    method: str,
    path: str,
    cookie_header: str,
    body: dict | None = None,
    user_agent: str | None = None,
) -> tuple[str, int, dict]:
    errors: list[str] = []
    for host in domain_list():
        try:
            status, payload = with_retry(
                lambda h=host: api(opener, h, method, path, cookie_header, body, user_agent)
            )
            return host, status, payload
        except ApiError as e:
            if not e.retryable:
                raise
            errors.append(f"{host}: {e.reason}")
        except Exception:
            errors.append(f"{host}: error")
    raise ApiError("all-domains-failed", retryable=True)


def is_auth_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    reason = str(payload.get("reason", "")).strip().lower()
    message = str(payload.get("message", "")).strip().lower()
    return (
        code == -2
        or payload.get("http_status") in (401, 403)
        or reason in {"device-mismatch", "http-auth-rejected"}
        or "没有权限" in message
        or "permission" in message
        or "unauthorized" in message
        or "automated check-in detected" in message
    )


def classify_checkin_result(payload: dict) -> str:
    if not isinstance(payload, dict):
        return "invalid"
    reason = str(payload.get("reason", "")).strip().lower()
    if reason == "checkin-challenge-unavailable":
        return "challenge-unavailable"
    if reason == "checkin-challenge-expired":
        return "challenge-expired"
    if is_auth_payload(payload):
        return "auth"
    message = str(payload.get("message", "")).strip().lower()
    code = payload.get("code")
    if code == 1 or "repeat" in message or "try tomorrow" in message or "return tomorrow" in message:
        return "already"
    if code == 0 or "checkin! got" in message or "observation logged" in message:
        return "success"
    return "failed"


def awarded_from_message(payload: dict) -> int | None:
    message = str(payload.get("message", ""))
    matched = _GOT_POINTS_RE.search(message)
    if not matched:
        return None
    return int(matched.group(1))


def build_proof(nonce: str) -> str:
    return hashlib.sha256((nonce + PROOF_SUFFIX).encode("utf-8")).hexdigest()


def parse_challenge(payload: dict) -> tuple[str, float] | None:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    nonce = payload.get("nonce")
    ttl = payload.get("ttl", 300000)
    if not isinstance(nonce, str) or not 0 < len(nonce) <= MAX_NONCE_LEN:
        return None
    if type(ttl) not in (int, float):
        return None
    if type(ttl) is float and not math.isfinite(ttl):
        return None
    if ttl <= CHECKIN_CHALLENGE_DELAY * 1000:
        return None
    return nonce, float(ttl)


def parse_iqi(payload: dict) -> dict:
    if payload.get("code") != 0:
        raise ValueError("iqi-code")
    quality = (payload.get("data") or {}).get("quality")
    if not isinstance(quality, dict) or "score" not in quality:
        raise ValueError("iqi-score-missing")
    score = quality["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("iqi-score-type")
    score_f = float(score)
    if not 0 <= score_f <= 100:
        raise ValueError("iqi-score-range")
    return {
        "score": score_f,
        "level": quality.get("level", ""),
        "bandwidth": quality.get("bandwidth"),
        "latency": quality.get("latency"),
    }


def get_iqi(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str | None = None,
) -> tuple[str, dict]:
    last = "iqi-failed"
    for host in domain_list():
        try:
            status, payload = with_retry(
                lambda h=host: api(opener, h, "GET", "/api/user/iqi", cookie_header, None, user_agent)
            )
            if status != 200:
                raise ApiError("iqi-http", retryable=True, http_status=status)
            return host, parse_iqi(payload)
        except ApiError as e:
            if not e.retryable:
                raise
            last = e.reason
        except ValueError as e:
            last = str(e)
    raise ApiError(last, retryable=True)


def _entry_is_today(item: dict, today: str) -> bool:
    detail = item.get("detail")
    if isinstance(detail, str) and detail == today:
        return True
    for key in ("time", "date", "createdAt", "created_at"):
        raw = item.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            seconds = float(raw) / 1000 if float(raw) > 10_000_000_000 else float(raw)
            try:
                day = datetime.fromtimestamp(seconds, TZ_CST).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                continue
            if day == today:
                return True
        if today in str(raw):
            return True
    return False


def _history_items(points_payload: dict) -> list:
    history = points_payload.get("history")
    if history is None and isinstance(points_payload.get("data"), dict):
        history = points_payload["data"].get("history")
    return history if isinstance(history, list) else []


def _positive_change(item: dict) -> float | None:
    change = item.get("change", item.get("points"))
    try:
        if change is None or isinstance(change, bool):
            return None
        value = float(change)
    except (TypeError, ValueError):
        return None
    if value > 0:
        return value
    return None


def _checkin_reason(item: dict) -> bool:
    reason = " ".join(str(item.get(key, "")) for key in ("reason", "type", "business")).lower()
    return any(key in reason for key in ("check", "sign", "daily", "observation"))


def already_checked_in_today(points_payload: dict, now: datetime) -> bool:
    if points_payload.get("code") not in (None, 0):
        return False
    today = now.astimezone(TZ_CST).strftime("%Y-%m-%d")
    for item in _history_items(points_payload):
        if not isinstance(item, dict) or not _entry_is_today(item, today):
            continue
        if _positive_change(item) is not None or _checkin_reason(item):
            return True
    return False


def today_positive_change(points_payload: dict, now: datetime) -> float | None:
    today = now.astimezone(TZ_CST).strftime("%Y-%m-%d")
    checkin_values: list[float] = []
    other_values: list[float] = []
    for item in _history_items(points_payload):
        if not isinstance(item, dict) or not _entry_is_today(item, today):
            continue
        value = _positive_change(item)
        if value is None:
            continue
        labeled = any(str(item.get(key, "")).strip() for key in ("reason", "type", "business"))
        if _checkin_reason(item) or not labeled:
            checkin_values.append(value)
        else:
            other_values.append(value)
    chosen = checkin_values or other_values
    if not chosen:
        return None
    return max(chosen)


def assess_reward(change: float | None, awarded: int | None) -> str:
    values = [value for value in (change, awarded) if value is not None]
    if not values:
        return "unverified"
    if any(value <= LOW_REWARD_MAX for value in values):
        return "low"
    return "ok"


def log_reward(change: float | None, awarded: int | None, *, already: bool) -> None:
    kind = assess_reward(change, awarded)
    if kind == "low":
        detail: list[str] = []
        if change is not None:
            detail.append(f"history={change:g}")
        if awarded is not None:
            detail.append(f"response={awarded}")
        log("低奖励异常 " + " ".join(detail) + "，不重复签到。")
        return
    if kind == "ok" and change is not None:
        prefix = "今日已有入账" if already else "Points History 核验入账"
        log(f"{prefix} {change:g}")
        return
    if kind == "ok" and awarded is not None:
        log(f"签到响应奖励 {awarded}，Points History 未返回今日记录，不重复签到")
        return
    if already:
        log("接口表示今日已签，Points History 未核到正向入账，不重复签到")
    else:
        log("Points History 未核到今日正向入账，不重复签到")


def summarize_status(payload: dict) -> str:
    data = payload.get("data") or {}
    days = data.get("leftDays")
    try:
        if days is not None and not isinstance(days, bool):
            return f"剩余 {float(days):.0f} 天"
    except (TypeError, ValueError):
        pass
    return "无 leftDays 字段"


def fetch_challenge(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
) -> tuple[str, str, float]:
    for host in domain_list():
        try:
            _status, payload = api(
                opener, host, "GET", "/api/user/checkin/challenge", cookie_header, None, user_agent
            )
        except ApiError as e:
            if not e.retryable:
                raise
            continue
        if is_auth_payload(payload):
            raise ApiError("auth-rejected", retryable=False)
        parsed = parse_challenge(payload)
        if parsed is None:
            continue
        nonce, ttl = parsed
        register_secret(nonce)
        return host, nonce, ttl
    raise ApiError("checkin-challenge-unavailable", retryable=True)


def checkin_once(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
) -> tuple[str, int, dict]:
    host, nonce, ttl = fetch_challenge(opener, cookie_header, user_agent)
    proof = build_proof(nonce)
    register_secret(proof)
    received_at = time.monotonic()
    time.sleep(CHECKIN_CHALLENGE_DELAY)
    if (time.monotonic() - received_at) * 1000 >= ttl:
        raise ApiError("checkin-challenge-expired", retryable=True)
    status, payload = api(
        opener,
        host,
        "POST",
        "/api/user/checkin",
        cookie_header,
        {"token": host, "nonce": nonce, "proof": proof},
        user_agent,
    )
    return host, status, payload


def verify_points(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
    awarded: int | None,
    *,
    already: bool,
) -> None:
    try:
        _host, _status, payload = api_with_domains(
            opener, "GET", "/api/user/points", cookie_header, None, user_agent
        )
    except Exception:
        log("Points History 查询失败，不重复签到")
        return
    log_reward(today_positive_change(payload, now), awarded, already=already)
    total = payload.get("points")
    try:
        if total is not None and not isinstance(total, bool):
            log(f"积分 {int(float(total))}")
    except (TypeError, ValueError):
        pass


def emit_day_lock(acquired: bool) -> None:
    """打印给 GitHub Actions 的当日成功锁。acquired 只表示今天不用再签。"""
    print(f"GLADOS_DAY_LOCK={'acquired' if acquired else 'open'}", flush=True)


def lock_dir() -> Path:
    override = os.environ.get("GLADOS_LOCK_DIR", "").strip()
    return Path(override) if override else ROOT / ".glados-checkin-lock"


def day_key(now: datetime) -> str:
    return now.astimezone(TZ_CST).strftime("%Y-%m-%d")


def local_lock_path(now: datetime) -> Path:
    return lock_dir() / f"glados-checkin-success-{day_key(now)}"


def local_day_locked(now: datetime) -> bool:
    return local_lock_path(now).is_file()


def write_local_day_lock(now: datetime) -> None:
    path = local_lock_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"glados-checkin-success-{day_key(now)}\n", encoding="utf-8")


def force_requested() -> bool:
    return os.environ.get("GLADOS_FORCE", "").strip().lower() in {"1", "true", "yes", "on"}


def finish(code: int, *, lock: bool, now: datetime | None = None) -> int:
    if lock and now is not None:
        write_local_day_lock(now)
    emit_day_lock(lock)
    return code


def do_checkin(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
) -> int:
    for attempt in range(1, CHECKIN_ATTEMPTS + 1):
        try:
            host, status, payload = checkin_once(opener, cookie_header, user_agent)
        except ApiError as e:
            if not e.retryable:
                log(f"签到认证失败，停止重试 reason={e.reason}")
                return 1
            log(f"签到第 {attempt}/{CHECKIN_ATTEMPTS} 次未提交成功 reason={e.reason}")
            if attempt < CHECKIN_ATTEMPTS:
                time.sleep(2 * attempt)
            continue

        kind = classify_checkin_result(payload)
        code = payload.get("code")
        code_text = str(code) if type(code) is int else "none"
        log(f"checkin host={host} http={status} class={kind} code={code_text}")
        if kind == "auth":
            log("签到认证失败，停止重试")
            return 1
        if kind in {"success", "already"}:
            try:
                _host, _status, status_payload = api_with_domains(
                    opener, "GET", "/api/user/status", cookie_header, None, user_agent
                )
                log(f"status {summarize_status(status_payload)}")
            except Exception:
                log("查询 status 失败")
            awarded = awarded_from_message(payload) if kind == "success" else None
            verify_points(
                opener,
                cookie_header,
                user_agent,
                now,
                awarded,
                already=kind == "already",
            )
            return 0
        log(f"签到结果未成功 class={kind}")
        if attempt < CHECKIN_ATTEMPTS:
            time.sleep(2 * attempt)
    log("签到失败：重试后仍未成功。认证错误不会继续重试。")
    return 1


def run(now: datetime | None = None) -> int:
    load_dotenv()
    now = now or beijing_now()
    if local_day_locked(now) and not force_requested():
        log(f"今日 {day_key(now)} 已有本地成功标记，跳过 GLaDOS API")
        return finish(0, lock=True, now=now)
    try:
        ck, kind = load_cookie()
        user_agent = resolve_user_agent()
    except ConfigError as e:
        log(str(e))
        return finish(2, lock=False, now=now)

    proxy = detect_proxy()
    opener = make_opener(proxy)
    log(f"运行检查 北京时间={now:%H:%M} proxy={proxy or 'direct'} ua={describe_user_agent()}")
    if kind == "koa":
        log("Cookie 为旧版 koa:sess 对。2026 接口可能拒绝，建议改用 gld:sess 与 gld:sess.sig。")

    threshold = get_iqi_threshold(now)
    if threshold is None:
        log("不在 16:00–19:59 签到窗口，退出")
        return finish(0, lock=False, now=now)

    log(f"当前档位: {schedule_label(threshold)}")

    points_payload: dict | None
    try:
        _host, _status, points_payload = api_with_domains(
            opener, "GET", "/api/user/points", ck, None, user_agent
        )
    except ApiError as e:
        if not e.retryable:
            log(f"查询今日签到状态被拒绝，停止 reason={e.reason}")
            return finish(1, lock=False, now=now)
        points_payload = None
    except Exception:
        points_payload = None

    if points_payload is not None and already_checked_in_today(points_payload, now):
        log_reward(today_positive_change(points_payload, now), None, already=True)
        log("今日已签到，退出")
        return finish(0, lock=True, now=now)
    if points_payload is None and threshold != "ANY":
        log("查询今日签到状态失败")
        return finish(2, lock=False, now=now)
    if points_payload is None:
        log("查询今日签到状态失败，19:00 anyscore 仍尝试兜底签到")

    score: float | None = None
    try:
        host, iqi = get_iqi(opener, ck, user_agent)
        score = iqi["score"]
        log(
            f"IQI host={host} score={score:g} level={iqi['level']} "
            f"bandwidth={iqi['bandwidth']} latency={iqi['latency']}"
        )
    except ApiError as e:
        if not e.retryable:
            log(f"IQI 认证失败，停止 reason={e.reason}")
            return finish(1, lock=False, now=now)
        if threshold != "ANY":
            log("IQI 查询失败，等待下一档")
            return finish(0, lock=False, now=now)
        log("IQI 查询失败，19:00 anyscore 仍执行兜底签到")
    except Exception:
        if threshold != "ANY":
            log("IQI 查询失败，等待下一档")
            return finish(0, lock=False, now=now)
        log("IQI 查询失败，19:00 anyscore 仍执行兜底签到")

    if threshold != "ANY":
        assert score is not None
        if score >= threshold:
            log(
                f"Index {score:g} >= {threshold}（严格小于才签），"
                f"本次不签，等下一档计划任务"
            )
            return finish(0, lock=False, now=now)
        log(f"Index {score:g} < {threshold}（{schedule_label(threshold)}），执行签到")
    elif score is None:
        log(f"{schedule_label(threshold)}，IQI 不可用，执行兜底签到")
    else:
        log(f"Index {score:g}，{schedule_label(threshold)}，执行签到")
    code = do_checkin(opener, ck, user_agent, now)
    return finish(code, lock=code == 0, now=now)


def _test_thresholds() -> None:
    cases = [
        ("15:59", None),
        ("16:00", 80),
        ("16:59", 80),
        ("17:00", 85),
        ("18:00", 90),
        ("19:00", "ANY"),
        ("19:30", "ANY"),
        ("20:00", None),
    ]
    for hm, expected in cases:
        hour, minute = map(int, hm.split(":"))
        current = datetime(2026, 1, 1, hour, minute, tzinfo=TZ_CST)
        got = get_iqi_threshold(current)
        assert got == expected, f"{hm}: got {got!r}, want {expected!r}"
    print("threshold tests ok")


def main() -> int:
    try:
        return run()
    except Exception:
        log("异常: 未预期错误")
        emit_day_lock(False)
        return 2


if __name__ == "__main__":
    if "--test-threshold" in sys.argv:
        _test_thresholds()
        raise SystemExit(0)
    if "--force" in sys.argv:
        os.environ["GLADOS_FORCE"] = "1"
    raise SystemExit(main())
