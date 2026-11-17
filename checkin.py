#!/usr/bin/env python3
"""GLaDOS v0.2：IQI 四档签到。API 仍提交 challenge/nonce/proof；遇到 Turnstile 时改由本机浏览器打开官方页面。Cookie 只从环境变量或 .env 读取。"""

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

# 每天 4 档（对应计划任务 00:01 / 00:31 / 01:01 / 01:31），值为从 0:00 起的分钟数
CHECKIN_SCHEDULE = (
    (0 * 60 + 1, 90),
    (0 * 60 + 31, 95),
    (1 * 60 + 1, 100),
    (1 * 60 + 31, None),
)
WINDOW_START_MINUTES = 0 * 60 + 1
WINDOW_END_MINUTES = 1 * 60 + 59

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
        return "01:31 最后一次补签 无条件"
    if threshold == 90:
        return "00:01 首次签到 Index<90 (+9)"
    if threshold == 95:
        return "00:31 第一次补签 Index<95 (+7)"
    if threshold == 100:
        return "01:01 第二次补签 Index<100 (+5)"
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
    """按当前北京时间所在档位返回 Index 上界或 ANY（01:31 任意 Index 签到）。"""
    minutes = minutes_of_day(now)
    if minutes < WINDOW_START_MINUTES or minutes > WINDOW_END_MINUTES:
        return None
    result: ThresholdResult = None
    for start_minutes, bound in CHECKIN_SCHEDULE:
        if minutes >= start_minutes:
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
    code = payload.get("code")
    if reason == "turnstile" or (code == 5 and "turnstile" in reason):
        return "turnstile"
    if is_auth_payload(payload):
        return "auth"
    message = str(payload.get("message", "")).strip().lower()
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
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "glados-checkin" / "state"
    return ROOT / ".glados-checkin-lock"


def day_key(now: datetime) -> str:
    return now.astimezone(TZ_CST).strftime("%Y-%m-%d")


def local_lock_path(now: datetime) -> Path:
    return lock_dir() / f"glados-checkin-success-{day_key(now)}"


def unknown_lock_path(now: datetime) -> Path:
    return lock_dir() / f"glados-checkin-unknown-{day_key(now)}"


def process_lock_path() -> Path:
    return lock_dir() / "glados-checkin-process.lock"


def local_day_locked(now: datetime) -> bool:
    return local_lock_path(now).is_file()


def unknown_locked(now: datetime) -> bool:
    return unknown_lock_path(now).is_file()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def clear_unknown_lock(now: datetime) -> None:
    try:
        unknown_lock_path(now).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def write_unknown_lock(now: datetime, reason: str) -> None:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", reason):
        reason = "unspecified"
    _atomic_write(unknown_lock_path(now), f"unknown {day_key(now)} reason={reason}\n")


def write_local_day_lock(now: datetime) -> None:
    stamp = now.astimezone(TZ_CST).strftime("%Y-%m-%d %H:%M:%S")
    _atomic_write(local_lock_path(now), f"glados-checkin-success-{day_key(now)} at={stamp}\n")
    clear_unknown_lock(now)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_lock_is_stale() -> bool:
    path = process_lock_path()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not text:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age > 2
    try:
        owner = int(text)
    except ValueError:
        return True
    return not _pid_alive(owner)


def acquire_process_lock() -> bool:
    path = process_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not process_lock_is_stale():
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        else:
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return True
    return False


def release_process_lock() -> None:
    path = process_lock_path()
    try:
        owner = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if owner != os.getpid():
        return
    try:
        path.unlink()
    except OSError:
        return


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


POST_UNCERTAIN_REASONS = {"network", "http-transient", "invalid-response", "all-domains-failed"}


def slot_of(now: datetime) -> str:
    minutes = minutes_of_day(now)
    if minutes < 31:
        return "00:01"
    if minutes < 61:
        return "00:31"
    if minutes < 91:
        return "01:01"
    return "01:31"


def index_text(score: float | None) -> str:
    if score is None:
        return "na"
    return f"{score:g}"


def log_event(
    event: str,
    *,
    slot: str,
    index: str,
    channel: str,
    retry: int,
    result: str,
    lock_written: bool,
) -> None:
    lock_text = "yes" if lock_written else "no"
    log(
        f"{event} slot={slot} index={index} channel={channel} "
        f"retry={retry} result={result} lock={lock_text}"
    )


def readonly_requested() -> bool:
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        return True
    return os.environ.get("GLADOS_READONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def account_email(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    host: str,
) -> str | None:
    try:
        status, payload = api(opener, host, "GET", "/api/user/status", cookie_header, None, user_agent)
    except Exception:
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    email = data.get("email")
    if not isinstance(email, str):
        return None
    email = email.strip()
    if not email or email in {"?", "Unknown"}:
        return None
    register_secret(email)
    return email


def submission_visible(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
) -> str:
    try:
        status, payload = api(
            opener, domain_list()[0], "GET", "/api/user/points", cookie_header, None, user_agent
        )
    except Exception:
        return "unknown"
    if status != 200 or not isinstance(payload, dict) or payload.get("code") not in (None, 0):
        return "unknown"
    if already_checked_in_today(payload, now):
        return "already"
    return "absent"


def finalize_accepted(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
    payload: dict,
    *,
    already: bool,
    channel: str,
    score: float | None,
    attempt: int,
) -> int:
    try:
        _host, _status, status_payload = api_with_domains(
            opener, "GET", "/api/user/status", cookie_header, None, user_agent
        )
        log(f"status {summarize_status(status_payload)}")
    except Exception:
        log("查询 status 失败")
    awarded = None if already else awarded_from_message(payload)
    verify_points(opener, cookie_header, user_agent, now, awarded, already=already)
    if already:
        event = "ALREADY_CHECKED_IN"
        result = "already"
    elif channel == "browser":
        event = "BROWSER_CHECKIN_SUCCESS"
        result = "success"
    else:
        event = "API_CHECKIN_SUCCESS"
        result = "success"
    log_event(
        event,
        slot=slot_of(now),
        index=index_text(score),
        channel=channel,
        retry=attempt,
        result=result,
        lock_written=True,
    )
    return 0


def perform_browser_checkin(*, host: str, expected_email: str, on_before_submit, emit, log) -> dict:
    from browser_checkin import run_browser_checkin

    return run_browser_checkin(
        host=host,
        expected_email=expected_email,
        on_before_submit=on_before_submit,
        emit=emit,
        log=log,
    )


def _browser_event(slot: str, score: float | None, attempt: int, event: str) -> None:
    log_event(
        event,
        slot=slot,
        index=index_text(score),
        channel="browser",
        retry=attempt,
        result="pending" if event == "TURNSTILE_VERIFICATION_PENDING" else "started",
        lock_written=False,
    )


def _remember_unknown(now: datetime, slot: str, score: float | None, attempt: int, channel: str) -> int:
    if not unknown_locked(now):
        write_unknown_lock(now, f"{channel}-unknown")
    log("签到请求可能已发出，但结果无法确认")
    log_event(
        "OUTCOME_UNKNOWN",
        slot=slot,
        index=index_text(score),
        channel=channel,
        retry=attempt,
        result="unknown",
        lock_written=False,
    )
    return 2


def submit_browser(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
    score: float | None,
    host: str,
) -> int:
    slot = slot_of(now)
    email = account_email(opener, cookie_header, user_agent, host)
    if email is None:
        log("无法核对浏览器登录身份，未点击签到")
        return 1
    for attempt in range(1, CHECKIN_ATTEMPTS + 1):
        def emit(event: str, attempt: int = attempt) -> None:
            _browser_event(slot, score, attempt, event)

        def on_before_submit() -> None:
            write_unknown_lock(now, "browser-submit")

        try:
            result = perform_browser_checkin(
                host=host,
                expected_email=email,
                on_before_submit=on_before_submit,
                emit=emit,
                log=log,
            )
        except Exception:
            return _remember_unknown(now, slot, score, attempt, "browser")
        if not isinstance(result, dict):
            return _remember_unknown(now, slot, score, attempt, "browser")
        failure = result.get("failure")
        submitted = bool(result.get("submitted"))
        payload = result.get("payload")
        if failure == "identity_unverified" and not submitted:
            log("浏览器登录身份与当前 Cookie 不一致，未点击签到")
            return 1
        if failure == "unknown" or (submitted and not isinstance(payload, dict)):
            return _remember_unknown(now, slot, score, attempt, "browser")
        if not submitted and failure in {"launch_failed", "button_unavailable", "pending"}:
            if failure == "pending":
                log("Turnstile 仍需人工处理，本次未提交签到")
            else:
                log("浏览器未能启动或签到按钮不可用，未提交签到")
            log_event(
                "RETRYABLE_FAILURE",
                slot=slot,
                index=index_text(score),
                channel="browser",
                retry=attempt,
                result="failed",
                lock_written=False,
            )
            if attempt < CHECKIN_ATTEMPTS:
                time.sleep(2 * attempt)
            continue
        if not isinstance(payload, dict):
            return _remember_unknown(now, slot, score, attempt, "browser")
        kind = classify_checkin_result(payload)
        code = payload.get("code")
        code_text = str(code) if type(code) is int else "none"
        log(f"browser checkin class={kind} code={code_text}")
        if kind in {"success", "already"}:
            return finalize_accepted(
                opener,
                cookie_header,
                user_agent,
                now,
                payload,
                already=kind == "already",
                channel="browser",
                score=score,
                attempt=attempt,
            )
        if kind == "auth":
            clear_unknown_lock(now)
            log("浏览器签到认证失败，停止重试")
            return 1
        if kind in {"turnstile", "failed"}:
            clear_unknown_lock(now)
            log(f"浏览器签到被拒绝 class={kind}")
            log_event(
                "RETRYABLE_FAILURE",
                slot=slot,
                index=index_text(score),
                channel="browser",
                retry=attempt,
                result="failed",
                lock_written=False,
            )
            if attempt < CHECKIN_ATTEMPTS:
                time.sleep(2 * attempt)
            continue
        return _remember_unknown(now, slot, score, attempt, "browser")
    log("签到失败：重试后仍未成功。认证错误不会继续重试。")
    return 1


def submit_with_policy(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
    score: float | None,
) -> int:
    slot = slot_of(now)
    for attempt in range(1, CHECKIN_ATTEMPTS + 1):
        try:
            host, status, payload = checkin_once(opener, cookie_header, user_agent)
        except ApiError as exc:
            if not exc.retryable:
                log(f"签到认证失败，停止重试 reason={exc.reason}")
                return 1
            if exc.reason in POST_UNCERTAIN_REASONS:
                visible = submission_visible(opener, cookie_header, user_agent, now)
                if visible == "already":
                    log("请求异常后积分流水显示今日已签到")
                    return finalize_accepted(
                        opener,
                        cookie_header,
                        user_agent,
                        now,
                        {"code": 1},
                        already=True,
                        channel="api",
                        score=score,
                        attempt=attempt,
                    )
                if visible == "unknown":
                    write_unknown_lock(now, "api-timeout")
                    log("签到请求可能已发出，但结果无法确认")
                    log_event(
                        "OUTCOME_UNKNOWN",
                        slot=slot,
                        index=index_text(score),
                        channel="api",
                        retry=attempt,
                        result="unknown",
                        lock_written=False,
                    )
                    return 2
            log(f"签到第 {attempt}/{CHECKIN_ATTEMPTS} 次未提交成功 reason={exc.reason}")
            log_event(
                "RETRYABLE_FAILURE",
                slot=slot,
                index=index_text(score),
                channel="api",
                retry=attempt,
                result="failed",
                lock_written=False,
            )
            if attempt < CHECKIN_ATTEMPTS:
                time.sleep(2 * attempt)
            continue

        kind = classify_checkin_result(payload)
        code = payload.get("code")
        code_text = str(code) if type(code) is int else "none"
        log(f"checkin host={host} http={status} class={kind} code={code_text}")
        if kind == "turnstile":
            log("签到接口要求 Turnstile，停止重复调用 API，改用浏览器")
            log_event(
                "TURNSTILE_DETECTED",
                slot=slot,
                index=index_text(score),
                channel="api",
                retry=attempt,
                result="turnstile",
                lock_written=False,
            )
            return submit_browser(opener, cookie_header, user_agent, now, score, host)
        if kind == "auth":
            log("签到认证失败，停止重试")
            return 1
        if kind in {"success", "already"}:
            return finalize_accepted(
                opener,
                cookie_header,
                user_agent,
                now,
                payload,
                already=kind == "already",
                channel="api",
                score=score,
                attempt=attempt,
            )
        log(f"签到结果未成功 class={kind}")
        log_event(
            "RETRYABLE_FAILURE",
            slot=slot,
            index=index_text(score),
            channel="api",
            retry=attempt,
            result="failed",
            lock_written=False,
        )
        if attempt < CHECKIN_ATTEMPTS:
            time.sleep(2 * attempt)
    log("签到失败：重试后仍未成功。认证错误不会继续重试。")
    return 1


def reconcile_unknown(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
) -> int:
    slot = slot_of(now)
    log("今日结果未确认，只核对接积分流水，不提交签到")
    visible = submission_visible(opener, cookie_header, user_agent, now)
    if visible == "already":
        log("积分流水确认今日已签到")
        log_event(
            "ALREADY_CHECKED_IN",
            slot=slot,
            index="na",
            channel="none",
            retry=0,
            result="already",
            lock_written=True,
        )
        return finish(0, lock=True, now=now)
    log("仍无法确认今日签到结果，停止当天自动提交")
    log_event(
        "OUTCOME_UNKNOWN",
        slot=slot,
        index="na",
        channel="none",
        retry=0,
        result="unknown",
        lock_written=False,
    )
    return finish(2, lock=False, now=now)


def run_readonly(
    opener: urllib.request.OpenerDirector,
    cookie_header: str,
    user_agent: str,
    now: datetime,
) -> int:
    log("只读诊断，不提交签到，不启动浏览器")
    code = 0
    try:
        _host, _status, points = api_with_domains(
            opener, "GET", "/api/user/points", cookie_header, None, user_agent
        )
        if already_checked_in_today(points, now):
            log("只读诊断：积分流水显示今日已有签到记录")
        else:
            log("只读诊断：积分流水未显示今日签到")
    except ApiError as exc:
        log(f"只读诊断失败 reason={exc.reason}")
        code = 1 if not exc.retryable else 2
    except Exception:
        log("只读诊断失败")
        code = 2
    if code == 0:
        try:
            host, iqi = get_iqi(opener, cookie_header, user_agent)
            log(
                f"IQI host={host} score={iqi['score']:g} level={iqi['level']} "
                f"bandwidth={iqi['bandwidth']} latency={iqi['latency']}"
            )
        except Exception:
            log("只读诊断未能读取 Index")
        try:
            _host, _status, status_payload = api_with_domains(
                opener, "GET", "/api/user/status", cookie_header, None, user_agent
            )
            log(f"status {summarize_status(status_payload)}")
        except Exception:
            log("只读诊断未能读取 status")
    return finish(code, lock=False, now=now)


def _run_locked(now: datetime) -> int:
    if local_day_locked(now) and not force_requested():
        log(f"今日 {day_key(now)} 已有本地成功标记，跳过 GLaDOS API")
        log_event(
            "DAY_LOCKED",
            slot=slot_of(now),
            index="na",
            channel="none",
            retry=0,
            result="skip",
            lock_written=True,
        )
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
    if readonly_requested():
        return run_readonly(opener, ck, user_agent, now)
    if unknown_locked(now):
        return reconcile_unknown(opener, ck, user_agent, now)

    threshold = get_iqi_threshold(now)
    if threshold is None:
        log("不在 00:01–01:59 签到窗口，退出")
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
        log_event(
            "ALREADY_CHECKED_IN",
            slot=slot_of(now),
            index="na",
            channel="none",
            retry=0,
            result="already",
            lock_written=True,
        )
        return finish(0, lock=True, now=now)
    if points_payload is None and threshold != "ANY":
        log("查询今日签到状态失败")
        return finish(2, lock=False, now=now)
    if points_payload is None:
        log("查询今日签到状态失败，01:31 无条件仍尝试兜底签到")

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
        log("IQI 查询失败，01:31 无条件仍执行兜底签到")
    except Exception:
        if threshold != "ANY":
            log("IQI 查询失败，等待下一档")
            return finish(0, lock=False, now=now)
        log("IQI 查询失败，01:31 无条件仍执行兜底签到")

    if threshold != "ANY":
        assert score is not None
        if score >= threshold:
            log(
                f"Index {score:g} >= {threshold}（严格小于才签），"
                f"本次不签，等下一档计划任务"
            )
            log_event(
                "INDEX_SKIP",
                slot=slot_of(now),
                index=index_text(score),
                channel="none",
                retry=0,
                result="skip",
                lock_written=False,
            )
            return finish(0, lock=False, now=now)
        log(f"Index {score:g} < {threshold}（{schedule_label(threshold)}），执行签到")
    elif score is None:
        log(f"{schedule_label(threshold)}，IQI 不可用，执行兜底签到")
    else:
        log(f"Index {score:g}，{schedule_label(threshold)}，执行签到")
    code = submit_with_policy(opener, ck, user_agent, now, score)
    return finish(code, lock=code == 0, now=now)


def run(now: datetime | None = None) -> int:
    load_dotenv()
    now = now or beijing_now()
    if not acquire_process_lock():
        log("已有签到进程正在运行，本次退出")
        log_event(
            "DAY_LOCKED",
            slot=slot_of(now),
            index="na",
            channel="none",
            retry=0,
            result="busy",
            lock_written=False,
        )
        emit_day_lock(False)
        return 0
    try:
        return _run_locked(now)
    finally:
        release_process_lock()


def init_browser_session() -> int:
    load_dotenv()
    if not acquire_process_lock():
        log("已有签到进程正在运行，未打开浏览器")
        return 1
    try:
        from browser_checkin import init_browser

        return init_browser(domain_list()[0], log)
    except Exception:
        log("浏览器初始化失败")
        return 1
    finally:
        release_process_lock()


def _test_thresholds() -> None:
    cases = [
        ("00:00", None),
        ("00:01", 90),
        ("00:30", 90),
        ("00:31", 95),
        ("01:01", 100),
        ("01:31", "ANY"),
        ("01:45", "ANY"),
        ("02:00", None),
    ]
    for hm, expected in cases:
        hour, minute = map(int, hm.split(":"))
        current = datetime(2026, 1, 1, hour, minute, tzinfo=TZ_CST)
        got = get_iqi_threshold(current)
        assert got == expected, f"{hm}: got {got!r}, want {expected!r}"
    print("threshold tests ok")


def main() -> int:
    try:
        if "--init-browser" in sys.argv:
            return init_browser_session()
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
