import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import browser_checkin
import checkin
from test_checkin_v02 import FlowTests, at

ROOT = Path(__file__).resolve().parents[1]
EMAIL = "hidden@example.test"


class Route:
    def __init__(self, url: str, method: str = "POST") -> None:
        self.request = SimpleNamespace(url=url, method=method)
        self.continued = 0
        self.aborted = 0

    def continue_(self) -> None:
        self.continued += 1

    def abort(self) -> None:
        self.aborted += 1


class FakeButton:
    def __init__(self, page) -> None:
        self.page = page
        self.disabled = False

    def is_visible(self) -> bool:
        return True

    def is_disabled(self) -> bool:
        return self.disabled

    def click(self) -> None:
        for _ in range(2):
            route = Route(self.page.base + "/api/user/checkin")
            self.page.context.guard(route)
            self.page.routes.append(route)


class FakePage:
    def __init__(self, context, outcome) -> None:
        self.context = context
        self.outcome = outcome
        self.base = context.base
        self.url = self.base + browser_checkin.CHECKIN_PAGE_PATH
        self.button = FakeButton(self)
        self.routes: list[Route] = []
        self.closed = False

    def goto(self, *args, **kwargs) -> None:
        return None

    def get_by_role(self, *args, **kwargs):
        return SimpleNamespace(first=self.button)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def expect_response(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    @property
    def value(self):
        return SimpleNamespace(json=self._json)

    def _json(self):
        if self.outcome == "invalid":
            raise ValueError("bad json")
        return self.outcome

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.outcome == "timeout":
            raise TimeoutError("lost response")
        return False


class FakeContext:
    def __init__(self, outcome, email: str = EMAIL) -> None:
        self.base = "https://glados.cloud"
        self.email = email
        self.page = FakePage(self, outcome)
        self.guard = None
        self.request = SimpleNamespace(get=self._get)

    def _get(self, *args, **kwargs):
        return SimpleNamespace(status=200, json=lambda: {"code": 0, "data": {"email": self.email}})

    def route(self, _pattern, guard) -> None:
        self.guard = guard

    def new_page(self):
        return self.page


def browser_result(**kwargs):
    return browser_checkin.run_on_context(
        kwargs.pop("context"),
        base="https://glados.cloud",
        expected_email=kwargs.pop("email", EMAIL),
        on_before_submit=kwargs.pop("on_before_submit"),
        emit=kwargs.pop("emit", lambda _event: None),
    )


class BrowserPageTests(unittest.TestCase):
    def test_one_post_and_known_json(self) -> None:
        context = FakeContext({"code": 0, "message": "Checkin! Got 9 Points"})
        marker = mock.Mock()
        result = browser_result(context=context, on_before_submit=marker)
        self.assertEqual(result["payload"]["code"], 0)
        self.assertEqual(result["failure"], None)
        self.assertEqual(sum(route.continued for route in context.page.routes), 1)
        self.assertEqual(sum(route.aborted for route in context.page.routes), 1)
        marker.assert_called_once()

    def test_lost_response_is_unknown_and_does_not_post_twice(self) -> None:
        context = FakeContext("timeout")
        marker = mock.Mock()
        result = browser_result(context=context, on_before_submit=marker)
        self.assertEqual(result["failure"], "unknown")
        self.assertTrue(result["submitted"])
        self.assertEqual(sum(route.continued for route in context.page.routes), 1)
        marker.assert_called_once()

    def test_invalid_json_stays_unknown(self) -> None:
        context = FakeContext("invalid")
        result = browser_result(context=context, on_before_submit=mock.Mock())
        self.assertEqual(result["failure"], "unknown")
        self.assertEqual(sum(route.continued for route in context.page.routes), 1)

    def test_identity_mismatch_does_not_click(self) -> None:
        context = FakeContext({"code": 0}, email="other@example.test")
        result = browser_result(context=context, on_before_submit=mock.Mock())
        self.assertEqual(result["failure"], "identity_unverified")
        self.assertEqual(context.page.routes, [])

    def test_pending_button_does_not_submit(self) -> None:
        context = FakeContext({"code": 0})
        context.page.button.disabled = True
        events = []
        with mock.patch.object(browser_checkin, "BUTTON_WAIT_MS", 0):
            result = browser_result(context=context, on_before_submit=mock.Mock(), emit=events.append)
        self.assertEqual(result["failure"], "pending")
        self.assertFalse(result["submitted"])
        self.assertEqual(context.page.routes, [])
        self.assertEqual(events, ["TURNSTILE_VERIFICATION_PENDING"])

    def test_exact_checkin_endpoint(self) -> None:
        base = "https://glados.cloud"
        self.assertTrue(browser_checkin.is_checkin_post(base + "/api/user/checkin", "POST", base))
        self.assertFalse(browser_checkin.is_checkin_post("https://glados.cloud.evil.invalid/api/user/checkin", "POST", base))
        self.assertFalse(browser_checkin.is_checkin_post(base + "/api/user/checkin/challenge", "POST", base))
        self.assertFalse(browser_checkin.is_checkin_post(base + "/api/user/checkin", "GET", base))

    def test_launch_uses_installed_browser_without_stealth(self) -> None:
        playwright = mock.Mock()
        playwright.chromium.launch_persistent_context.return_value = object()
        _context, channel = browser_checkin.launch_context(playwright, "C:/unused-profile")
        self.assertEqual(channel, "chrome")
        kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertFalse(kwargs["headless"])
        self.assertEqual(kwargs["channel"], "chrome")
        self.assertNotIn("--disable-blink-features=AutomationControlled", kwargs["args"])
        self.assertNotIn("user_agent", kwargs)

    def test_launch_falls_back_to_edge_only(self) -> None:
        playwright = mock.Mock()

        def launch(**kwargs):
            if kwargs["channel"] == "chrome":
                raise RuntimeError("missing")
            return object()

        playwright.chromium.launch_persistent_context.side_effect = launch
        _context, channel = browser_checkin.launch_context(playwright, "C:/unused-profile")
        self.assertEqual(channel, "msedge")
        channels = [call.kwargs["channel"] for call in playwright.chromium.launch_persistent_context.call_args_list]
        self.assertEqual(channels, ["chrome", "msedge"])

    def test_profile_dir_stays_outside_the_repo(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": r"C:\Users\example\AppData\Local", "GLADOS_BROWSER_PROFILE": ""},
            clear=False,
        ):
            path = browser_checkin.profile_dir()
        self.assertEqual(path, Path(r"C:\Users\example\AppData\Local\glados-checkin\browser-profile"))
        self.assertFalse(str(path).startswith(str(ROOT)))

    def test_actions_and_readonly_do_not_launch(self) -> None:
        for env in ({"GITHUB_ACTIONS": "true"}, {"GLADOS_READONLY": "1"}):
            with mock.patch.dict(os.environ, env, clear=False):
                result = browser_checkin.run_browser_checkin(
                    host="glados.cloud",
                    expected_email=EMAIL,
                    on_before_submit=mock.Mock(),
                    emit=lambda _event: None,
                    log=lambda _message: None,
                )
            self.assertEqual(result["failure"], "launch_failed")
            self.assertFalse(result["submitted"])


class OrchestrationTests(FlowTests):
    def setUp(self) -> None:
        super().setUp()
        self.browser_script = []
        self.browser_calls = []
        patcher = mock.patch("checkin.perform_browser_checkin", self.fake_browser)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_browser(self, **kwargs):
        if not self.browser_script:
            raise AssertionError("browser started unexpectedly")
        item = self.browser_script.pop(0)
        self.browser_calls.append(kwargs["host"])
        if item.get("submitted"):
            kwargs["on_before_submit"]()
        return item

    def test_turnstile_uses_browser_once(self) -> None:
        self.checkin_code = 5
        self.checkin_reason = "turnstile"
        self.checkin_message = ""
        self.browser_script = [
            {"submitted": True, "payload": {"code": 0, "message": "Checkin! Got 9 Points"}, "failure": None}
        ]
        self.after_checkin = 9
        self.assertEqual(checkin.run(at(0, 1)), 0)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.browser_calls, ["glados.cloud"])
        self.assertTrue(checkin.local_day_locked(at(0, 31)))
        self.assertFalse(checkin.unknown_locked(at(0, 31)))
        text = self.log_text()
        self.assertIn("TURNSTILE_DETECTED", text)
        self.assertIn("BROWSER_CHECKIN_SUCCESS", text)
        self.assertNotIn(EMAIL, text)
        self.assertNotIn("synthetic-private", text)

    def test_api_success_does_not_start_browser(self) -> None:
        self.after_checkin = 9
        self.assertEqual(checkin.run(at(0, 10)), 0)
        self.assertEqual(self.browser_calls, [])
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("API_CHECKIN_SUCCESS", self.log_text())
        self.assertTrue(checkin.local_day_locked(at(0, 10)))

    def test_already_checked_in_skips_all_four_slots(self) -> None:
        self.points_change = 9
        for moment in (at(0, 1), at(0, 31), at(1, 1), at(1, 31)):
            self.assertEqual(checkin.run(moment), 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.browser_calls, [])
        self.assertIn("ALREADY_CHECKED_IN", self.log_text())
        self.assertIn("DAY_LOCKED", self.log_text())

    def test_unknown_blocks_later_slots_and_resets_next_day(self) -> None:
        self.checkin_code = 5
        self.checkin_reason = "turnstile"
        self.browser_script = [{"submitted": True, "payload": None, "failure": "unknown"}]
        self.assertEqual(checkin.run(at(0, 1)), 2)
        self.assertTrue(checkin.unknown_locked(at(1, 31)))
        self.assertFalse(checkin.local_day_locked(at(1, 31)))
        self.calls.clear()
        self.browser_calls.clear()
        self.browser_script = [
            {"submitted": True, "payload": {"code": 0, "message": "Checkin! Got 9 Points"}, "failure": None}
        ]
        self.assertEqual(checkin.run(at(1, 31)), 2)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.browser_calls, [])
        self.assertIn("OUTCOME_UNKNOWN", self.log_text())
        next_day = at(0, 1).replace(day=10)
        self.assertFalse(checkin.unknown_locked(next_day))
        self.checkin_code = 0
        self.checkin_reason = ""
        self.after_checkin = 9
        self.assertEqual(checkin.run(next_day), 0)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.browser_calls, [])

    def test_launch_failure_is_not_success(self) -> None:
        self.checkin_code = 5
        self.checkin_reason = "turnstile"
        self.browser_script = [
            {"submitted": False, "payload": None, "failure": "launch_failed"} for _ in range(3)
        ]
        self.assertEqual(checkin.run(at(0, 1)), 1)
        self.assertEqual(len(self.browser_calls), 3)
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse(checkin.local_day_locked(at(0, 1)))
        self.assertFalse(checkin.unknown_locked(at(0, 1)))
        self.assertIn("RETRYABLE_FAILURE", self.log_text())
        self.assertIn("浏览器未能启动", self.log_text())

    def test_explicit_turnstile_reject_retries_then_succeeds(self) -> None:
        self.checkin_code = 5
        self.checkin_reason = "turnstile"
        self.after_checkin = 9
        self.browser_script = [
            {"submitted": True, "payload": {"code": 5, "reason": "turnstile"}, "failure": None},
            {"submitted": True, "payload": {"code": 5, "reason": "turnstile"}, "failure": None},
            {"submitted": True, "payload": {"code": 0, "message": "Checkin! Got 9 Points"}, "failure": None},
        ]
        self.assertEqual(checkin.run(at(0, 31)), 0)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.browser_calls), 3)
        self.assertTrue(checkin.local_day_locked(at(0, 31)))
        self.assertFalse(checkin.unknown_locked(at(0, 31)))

    def test_index_96_skips_and_95_still_reaches_later_slots(self) -> None:
        self.iqi_score = 96
        self.assertEqual(checkin.run(at(0, 1)), 0)
        self.assertEqual(checkin.run(at(0, 31)), 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.log_text().count("INDEX_SKIP"), 2)
        self.iqi_score = 95
        self.after_checkin = 9
        self.assertEqual(checkin.run(at(1, 1)), 0)
        self.assertEqual(len(self.posts()), 1)

    def test_second_process_does_not_enter(self) -> None:
        self.assertTrue(checkin.acquire_process_lock())
        result = {}

        def worker() -> None:
            result["code"] = checkin.run(at(0, 1))
            result["calls"] = len(self.calls)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["calls"], 0)
        self.assertIn("已有签到进程正在运行", self.log_text())
        checkin.release_process_lock()

    def test_dead_process_lock_can_be_recovered(self) -> None:
        path = checkin.process_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("2000000001", encoding="utf-8")
        self.assertTrue(checkin.acquire_process_lock())
        self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))
        checkin.release_process_lock()
        self.assertFalse(path.exists())

    def test_confirmed_absent_network_error_can_retry(self) -> None:
        self.network_posts = 1

        def fake_api(opener, host, method, path, cookie_header, body=None, user_agent=None):
            if path == "/api/user/checkin" and self.network_posts:
                self.network_posts -= 1
                self.calls.append((host, method, path, body, cookie_header, user_agent))
                raise checkin.ApiError("network", retryable=True)
            return super(OrchestrationTests, self).fake_api(
                opener, host, method, path, cookie_header, body, user_agent
            )

        with mock.patch("checkin.api", fake_api):
            self.assertEqual(checkin.run(at(0, 10)), 0)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.browser_calls, [])
        self.assertTrue(checkin.local_day_locked(at(0, 10)))

    def test_unverified_network_error_does_not_retry_submit(self) -> None:
        self.points_reads = 0

        def fake_api(opener, host, method, path, cookie_header, body=None, user_agent=None):
            if path == "/api/user/points":
                self.points_reads += 1
                if self.points_reads > 1:
                    self.calls.append((host, method, path, body, cookie_header, user_agent))
                    raise checkin.ApiError("network", retryable=True)
            if path == "/api/user/checkin":
                self.calls.append((host, method, path, body, cookie_header, user_agent))
                raise checkin.ApiError("network", retryable=True)
            return super(OrchestrationTests, self).fake_api(
                opener, host, method, path, cookie_header, body, user_agent
            )

        with mock.patch("checkin.api", fake_api):
            self.assertEqual(checkin.run(at(1, 31)), 2)
            self.assertEqual(len(self.posts()), 1)
            self.calls.clear()
            self.assertEqual(checkin.run(at(1, 31)), 2)
        self.assertEqual(self.posts(), [])
        self.assertTrue(checkin.unknown_locked(at(1, 31)))
        self.assertIn("OUTCOME_UNKNOWN", self.log_text())

    def test_readonly_does_not_submit_or_lock(self) -> None:
        os.environ["GLADOS_READONLY"] = "1"
        self.assertEqual(checkin.run(at(0, 1)), 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.browser_calls, [])
        self.assertFalse(checkin.local_day_locked(at(0, 1)))
        self.assertIn("只读诊断", self.log_text())


class ScheduleFileTests(unittest.TestCase):
    def test_github_actions_has_no_schedule(self) -> None:
        text = (ROOT / ".github" / "workflows" / "checkin.yml").read_text(encoding="utf-8")
        self.assertNotIn("schedule:", text)
        self.assertNotIn("cron:", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("GLADOS_READONLY", text)
        self.assertNotIn("playwright", text.lower())

    def test_windows_task_keeps_four_slots_and_wake(self) -> None:
        text = (ROOT / "install-task.ps1").read_text(encoding="utf-8")
        for moment in ("00:01", "00:31", "01:01", "01:31"):
            self.assertIn(moment, text)
        self.assertIn("-WakeToRun", text)
        self.assertIn("Interactive", text)
        self.assertIn("IgnoreNew", text)

    def test_turnstile_classification(self) -> None:
        self.assertEqual(
            checkin.classify_checkin_result({"code": 5, "reason": "turnstile"}),
            "turnstile",
        )

    def test_retry_budget_constant(self) -> None:
        self.assertEqual(checkin.CHECKIN_ATTEMPTS, 3)
        self.assertEqual(
            checkin.CHECKIN_SCHEDULE,
            ((1, 90), (31, 95), (61, 100), (91, None)),
        )


if __name__ == "__main__":
    unittest.main()
