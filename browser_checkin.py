#!/usr/bin/env python3
"""用本机已安装的 Chrome 或 Edge 打开 GLaDOS 官方签到页。

页面自己完成 Turnstile 和签到请求。这里不计算 nonce、proof，也不填写 Turnstile token。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

BROWSER_CHANNELS = ("chrome", "msedge")
CHECKIN_PAGE_PATH = "/console/checkin"
BUTTON_WAIT_MS = 45_000
RESULT_WAIT_MS = 150_000
STATUS_TIMEOUT_MS = 15_000
PAGE_TIMEOUT_MS = 45_000
BUTTON_NAME = re.compile(r"^(check\s*in|签到)$", re.IGNORECASE)

Emit = Callable[[str], None]
Log = Callable[[str], None]
BeforeSubmit = Callable[[], None]


def profile_dir() -> Path:
    override = os.environ.get("GLADOS_BROWSER_PROFILE", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home())
    return Path(base) / "glados-checkin" / "browser-profile"


def official_base(host: str) -> str:
    raw = host.strip()
    if "://" in raw:
        raw = urlsplit(raw).netloc
    raw = raw.lower().strip(".")
    if not raw or not re.fullmatch(r"[a-z0-9.-]+", raw) or ".." in raw:
        raise ValueError("invalid-host")
    return f"https://{raw}"


def is_checkin_post(url: str, method: str, base: str) -> bool:
    if str(method).upper() != "POST":
        return False
    parsed = urlsplit(url)
    origin = urlsplit(base)
    return (
        parsed.scheme == origin.scheme
        and parsed.netloc == origin.netloc
        and parsed.path == "/api/user/checkin"
    )


def install_single_submit_guard(context, base: str, on_before_submit: BeforeSubmit) -> dict:
    """放行最多一个官方签到 POST，并在它发出前调用 on_before_submit。"""
    state = {"allowed": False, "submitted": False, "blocked": 0, "marker_failed": False}

    def guard(route) -> None:
        request = route.request
        if not is_checkin_post(request.url, request.method, base):
            route.continue_()
            return
        if not state["allowed"] or state["submitted"]:
            state["blocked"] += 1
            route.abort()
            return
        try:
            on_before_submit()
        except Exception:
            state["marker_failed"] = True
            route.abort()
            return
        state["submitted"] = True
        route.continue_()

    context.route("**/*", guard)
    state["guard"] = guard
    return state


def email_from_status(payload: object) -> str | None:
    if not isinstance(payload, dict):
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
    return email


def _status_email(context, base: str) -> str | None:
    response = context.request.get(base + "/api/user/status", timeout=STATUS_TIMEOUT_MS)
    if getattr(response, "status", None) != 200:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    return email_from_status(payload)


def _wait_until_enabled(page, button) -> bool:
    deadline = time.monotonic() + BUTTON_WAIT_MS / 1000
    while True:
        try:
            if button.is_visible() and not button.is_disabled():
                return True
        except Exception:
            return False
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


def _failure(failure: str, *, submitted: bool = False) -> dict:
    return {"submitted": submitted, "payload": None, "failure": failure}


def run_on_context(
    context,
    *,
    base: str,
    expected_email: str,
    on_before_submit: BeforeSubmit,
    emit: Emit,
) -> dict:
    """在已经打开的浏览器上下文中完成一次官方页面签到。"""
    state = install_single_submit_guard(context, base, on_before_submit)
    page = None
    try:
        if not isinstance(expected_email, str) or not expected_email.strip():
            return _failure("identity_unverified")
        try:
            actual = _status_email(context, base)
        except Exception:
            return _failure("identity_unverified")
        if actual != expected_email.strip():
            return _failure("identity_unverified")
        page = context.new_page()
        page.goto(base + CHECKIN_PAGE_PATH, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        current = urlsplit(page.url)
        origin = urlsplit(base)
        if current.scheme != origin.scheme or current.netloc != origin.netloc or current.path != CHECKIN_PAGE_PATH:
            return _failure("identity_unverified")
        button = page.get_by_role("button", name=BUTTON_NAME).first
        emit("TURNSTILE_VERIFICATION_PENDING")
        if not _wait_until_enabled(page, button):
            if state["submitted"]:
                return _failure("unknown", submitted=True)
            return _failure("pending")
        state["allowed"] = True
        clicked = False
        try:
            with page.expect_response(
                lambda response: is_checkin_post(response.url, response.request.method, base),
                timeout=RESULT_WAIT_MS,
            ) as info:
                clicked = True
                button.click()
            if state["marker_failed"]:
                return _failure("launch_failed")
            try:
                payload = info.value.json()
            except Exception:
                return _failure("unknown", submitted=True)
        except Exception:
            if state["marker_failed"]:
                return _failure("launch_failed")
            if state["submitted"] or clicked:
                return _failure("unknown", submitted=True)
            return _failure("button_unavailable")
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            return _failure("unknown", submitted=True)
        return {"submitted": True, "payload": payload, "failure": None}
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def launch_context(playwright, directory: str):
    errors: list[str] = []
    for channel in BROWSER_CHANNELS:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=directory,
                channel=channel,
                headless=False,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            return context, channel
        except Exception as exc:
            errors.append(f"{channel}:{type(exc).__name__}")
    raise RuntimeError("browser-unavailable:" + ",".join(errors))


def _guard_all_checkins(context, base: str) -> None:
    def guard(route) -> None:
        request = route.request
        if is_checkin_post(request.url, request.method, base):
            route.abort()
            return
        route.continue_()

    context.route("**/*", guard)


def _browser_disabled() -> str | None:
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        return "github-actions"
    if os.environ.get("GLADOS_READONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "readonly"
    return None


def run_browser_checkin(
    *,
    host: str,
    expected_email: str,
    on_before_submit: BeforeSubmit,
    emit: Emit,
    log: Log,
) -> dict:
    disabled = _browser_disabled()
    if disabled:
        return _failure("launch_failed")
    if not isinstance(expected_email, str) or not expected_email.strip():
        return _failure("identity_unverified")
    try:
        base = official_base(host)
    except ValueError:
        return _failure("launch_failed")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _failure("launch_failed")

    directory = profile_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            try:
                context, channel = launch_context(playwright, str(directory))
            except RuntimeError:
                return _failure("launch_failed")
            log(f"浏览器通道已启动 channel={channel}")
            emit("BROWSER_STARTED")
            try:
                return run_on_context(
                    context,
                    base=base,
                    expected_email=expected_email,
                    on_before_submit=on_before_submit,
                    emit=emit,
                )
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception:
        return _failure("launch_failed")


def init_browser(host: str, log: Log | None = None) -> int:
    """打开专用 Profile 让用户登录。不点击签到，并拦截签到 POST。"""
    write = log or (lambda message: print(message, flush=True))
    disabled = _browser_disabled()
    if disabled:
        write("当前环境不允许启动浏览器")
        return 1
    try:
        base = official_base(host)
    except ValueError:
        write("签到域名无效，未启动浏览器")
        return 1
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        write("未安装 playwright，未启动浏览器")
        return 1

    directory = profile_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            context, channel = launch_context(playwright, str(directory))
            try:
                _guard_all_checkins(context, base)
                page = context.new_page()
                page.goto(base + CHECKIN_PAGE_PATH, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                write(f"已打开登录窗口 channel={channel}。请在窗口中登录，不要点击签到。完成后回到终端按 Enter。")
                try:
                    input()
                except EOFError:
                    page.wait_for_timeout(300_000)
                try:
                    page.close()
                except Exception:
                    pass
            finally:
                context.close()
    except Exception:
        write("浏览器初始化失败")
        return 1
    write("浏览器登录窗口已关闭。本次没有点击签到。")
    return 0
