import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
from unittest import mock

import checkin

COOKIE = "gld:sess=synthetic-private-session; gld:sess.sig=synthetic-private-signature"
LEGACY_COOKIE = "koa:sess=synthetic-private-session; koa:sess.sig=synthetic-private-signature"
NONCE = "synthetic-nonce"
OTHER_NONCE = "another-nonce"
PROOF = "95d98116667d20076de5e31eeba57cb65dac0e9be21afb32263bac1178699533"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
CST = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 9, hour, minute, tzinfo=CST)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.jump: float | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds if self.jump is None else self.jump


class FakeOpener:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        result = self.handler(req)
        if isinstance(result, Exception):
            raise result
        status, payload = result
        body = json.dumps(payload).encode("utf-8")
        response = mock.Mock()
        response.status = status
        response.read.return_value = body
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response


def request_json(req) -> dict:
    return json.loads(req.data.decode("utf-8"))


def host_of(req) -> str:
    return req.full_url.split("/")[2]


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patches = [
            mock.patch("checkin.time.sleep", self.clock.sleep),
            mock.patch("checkin.time.monotonic", self.clock.monotonic),
            mock.patch.dict(
                os.environ,
                {
                    "GLADOS_USER_AGENT": UA,
                    "GLADOS_HOST": "",
                    "GLADOS_LOG_PATH": os.path.join(self.tmp.name, "checkin.log"),
                },
                clear=False,
            ),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        checkin.reset_secrets()

    def test_proof_vector(self) -> None:
        self.assertEqual(checkin.build_proof(NONCE), PROOF)

    def test_same_origin_submit_waits_and_keeps_cookie(self) -> None:
        def handler(req):
            if req.full_url.endswith("/api/user/checkin/challenge"):
                return 200, {"code": 0, "nonce": NONCE, "ttl": 300000}
            if req.full_url.endswith("/api/user/checkin"):
                return 200, {"code": 0, "message": "Checkin! Got 9 Points"}
            raise AssertionError(req.full_url)

        opener = FakeOpener(handler)
        host, status, payload = checkin.checkin_once(opener, COOKIE, UA)
        self.assertEqual(host, "glados.cloud")
        self.assertEqual(status, 200)
        self.assertEqual(checkin.classify_checkin_result(payload), "success")
        self.assertEqual(self.clock.slept, [2.7])
        challenge, post = opener.requests
        self.assertEqual(host_of(challenge), host_of(post))
        self.assertEqual(challenge.get_header("Cookie"), COOKIE)
        self.assertEqual(post.get_header("Cookie"), COOKIE)
        self.assertEqual(post.get_header("User-agent"), UA)
        self.assertEqual(post.get_header("Origin"), "https://glados.cloud")
        self.assertEqual(request_json(post), {"token": "glados.cloud", "nonce": NONCE, "proof": PROOF})

    def test_fallback_domain_binds_post_and_token(self) -> None:
        def handler(req):
            host = host_of(req)
            if req.full_url.endswith("/challenge"):
                if host == "glados.cloud":
                    raise checkin.urllib.error.URLError("down")
                return 200, {"code": 0, "nonce": NONCE, "ttl": 300000}
            self.assertEqual(host, "glados.rocks")
            return 200, {"code": 0, "message": "Checkin! Got 9 Points"}

        opener = FakeOpener(handler)
        host, _status, payload = checkin.checkin_once(opener, COOKIE, UA)
        self.assertEqual(host, "glados.rocks")
        self.assertEqual(checkin.classify_checkin_result(payload), "success")
        posts = [req for req in opener.requests if req.full_url.endswith("/api/user/checkin")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(request_json(posts[0])["token"], "glados.rocks")
        self.assertEqual(set(request_json(posts[0])), {"token", "nonce", "proof"})

    def test_short_ttl_never_sleeps_or_posts(self) -> None:
        def handler(req):
            self.assertTrue(req.full_url.endswith("/challenge"))
            return 200, {"code": 0, "nonce": NONCE, "ttl": 2700}

        opener = FakeOpener(handler)
        with self.assertRaises(checkin.ApiError) as caught:
            checkin.checkin_once(opener, COOKIE, UA)
        self.assertEqual(caught.exception.reason, "checkin-challenge-unavailable")
        self.assertEqual(self.clock.slept, [])
        self.assertFalse(any(req.full_url.endswith("/api/user/checkin") for req in opener.requests))

    def test_ttl_just_above_wait_submits_before_expiry(self) -> None:
        def handler(req):
            if req.full_url.endswith("/challenge"):
                return 200, {"code": 0, "nonce": NONCE, "ttl": 2701}
            return 200, {"code": 0}

        opener = FakeOpener(handler)
        _host, _status, payload = checkin.checkin_once(opener, COOKIE, UA)
        self.assertEqual(payload["code"], 0)
        self.assertEqual(self.clock.slept, [2.7])

    def test_exact_expiry_is_not_submitted_and_retry_uses_new_challenge(self) -> None:
        nonces = iter([NONCE, OTHER_NONCE])

        def handler(req):
            if req.full_url.endswith("/challenge"):
                return 200, {"code": 0, "nonce": next(nonces), "ttl": 3000}
            raise AssertionError("expired challenge was submitted")

        opener = FakeOpener(handler)
        self.clock.jump = 3
        with self.assertRaises(checkin.ApiError) as first:
            checkin.checkin_once(opener, COOKIE, UA)
        self.assertEqual(first.exception.reason, "checkin-challenge-expired")
        self.clock.now = 10
        with self.assertRaises(checkin.ApiError) as second:
            checkin.checkin_once(opener, COOKIE, UA)
        self.assertEqual(second.exception.reason, "checkin-challenge-expired")
        self.assertEqual(sum(1 for req in opener.requests if req.data), 0)

    def test_post_failure_retries_with_fresh_proof(self) -> None:
        state = {"posts": 0}

        def handler(req):
            if req.full_url.endswith("/challenge"):
                nonce = NONCE if state["posts"] == 0 else OTHER_NONCE
                return 200, {"code": 0, "nonce": nonce, "ttl": 300000}
            if req.full_url.endswith("/api/user/checkin"):
                state["posts"] += 1
                if state["posts"] == 1:
                    raise checkin.urllib.error.URLError("timeout")
                return 200, {"code": 0, "message": "Checkin! Got 9 Points"}
            if req.full_url.endswith("/api/user/status"):
                return 200, {"code": 0, "data": {"leftDays": 1}}
            if req.full_url.endswith("/api/user/points"):
                return 200, {
                    "code": 0,
                    "points": 109,
                    "history": [{"time": "2026-11-09 16:05:00", "change": 9, "reason": "checkin"}],
                }
            raise AssertionError(req.full_url)

        opener = FakeOpener(handler)
        code = checkin.do_checkin(opener, COOKIE, UA, at(16))
        self.assertEqual(code, 0)
        posts = [request_json(req) for req in opener.requests if req.data]
        self.assertEqual(len(posts), 2)
        self.assertNotEqual(posts[0]["nonce"], posts[1]["nonce"])
        self.assertNotEqual(posts[0]["proof"], posts[1]["proof"])
        self.assertEqual(posts[1]["proof"], checkin.build_proof(OTHER_NONCE))

    def test_auth_challenge_stops_without_post_or_domain_hop(self) -> None:
        def handler(req):
            self.assertEqual(host_of(req), "glados.cloud")
            return 200, {"code": -2, "message": "没有权限"}

        opener = FakeOpener(handler)
        self.assertEqual(checkin.do_checkin(opener, COOKIE, UA, at(16)), 1)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.clock.slept, [])

    def test_http_401_stops(self) -> None:
        def handler(req):
            raise checkin.urllib.error.HTTPError(
                req.full_url, 401, "denied", None, io.BytesIO(b"secret-body")
            )

        opener = FakeOpener(handler)
        self.assertEqual(checkin.do_checkin(opener, COOKIE, UA, at(16)), 1)
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_challenge_does_not_fall_back_to_token_only(self) -> None:
        payloads = (
            {"code": 0},
            {"code": 0, "nonce": ""},
            {"code": 0, "nonce": "x" * 4097},
            {"code": 0, "nonce": NONCE, "ttl": True},
            {"code": 0, "nonce": NONCE, "ttl": float("nan")},
        )
        for payload in payloads:
            opener = FakeOpener(lambda req, payload=payload: (200, payload))
            with self.assertRaises(checkin.ApiError):
                checkin.checkin_once(opener, COOKIE, UA)
            self.assertFalse(any(req.data for req in opener.requests))


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.points_change = None
        self.after_checkin = None
        self.iqi_error = None
        self.iqi_score = 70
        self.checkin_code = 0
        self.checkin_message = "Checkin! Got 9 Points"
        self.challenge_code = 0
        env = {
            "GLADOS_COOKIE": COOKIE,
            "GLADOS_USER_AGENT": UA,
            "GLADOS_LOG_PATH": os.path.join(self.tmp.name, "checkin.log"),
            "GLADOS_PROXY": "",
            "HTTPS_PROXY": "",
            "https_proxy": "",
            "GLADOS_HOST": "",
            "GLADOS_LOCK_DIR": os.path.join(self.tmp.name, "lock"),
            "GLADOS_FORCE": "",
        }
        self._patches = [
            mock.patch("checkin.time.sleep", self.clock.sleep),
            mock.patch("checkin.time.monotonic", self.clock.monotonic),
            mock.patch("checkin.detect_proxy", return_value=None),
            mock.patch("checkin.load_dotenv"),
            mock.patch("checkin.api", self.fake_api),
            mock.patch.dict(os.environ, env, clear=False),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        checkin.reset_secrets()

    def fake_api(self, opener, host, method, path, cookie_header, body=None, user_agent=None):
        self.calls.append((host, method, path, body, cookie_header, user_agent))
        self.assertNotIn("synthetic-private", json.dumps(body or {}))
        if path == "/api/user/points":
            change = self.points_change
            if self.after_checkin is not None and any(item[2] == "/api/user/checkin" for item in self.calls):
                change = self.after_checkin
            history = []
            if change is not None:
                history.append({"time": "2026-11-09 16:05:00", "change": change, "reason": "checkin"})
            return 200, {"code": 0, "points": 100, "history": history}
        if path == "/api/user/iqi":
            if self.iqi_error:
                raise self.iqi_error
            return 200, {"code": 0, "data": {"quality": {"score": self.iqi_score, "level": "poor"}}}
        if path == "/api/user/checkin/challenge":
            if self.challenge_code == -2:
                return 200, {"code": -2, "message": "没有权限"}
            nonce = NONCE if not any(item[1] == "POST" for item in self.calls) else OTHER_NONCE
            return 200, {"code": 0, "nonce": nonce, "ttl": 300000}
        if path == "/api/user/checkin":
            return 200, {"code": self.checkin_code, "message": self.checkin_message}
        if path == "/api/user/status":
            return 200, {"code": 0, "data": {"leftDays": 30, "email": "hidden@example.test"}}
        raise AssertionError(path)

    def posts(self):
        return [call for call in self.calls if call[2] == "/api/user/checkin"]

    def log_text(self) -> str:
        with open(os.environ["GLADOS_LOG_PATH"], encoding="utf-8") as handle:
            return handle.read()

    def test_threshold_matrix_and_strict_less_than(self) -> None:
        checkin._test_thresholds()
        self.iqi_score = 90
        self.assertEqual(checkin.run(at(0, 1)), 0)
        self.assertEqual(self.posts(), [])
        self.calls.clear()
        self.iqi_score = 89
        self.assertEqual(checkin.run(at(0, 1)), 0)
        self.assertEqual(len(self.posts()), 1)
        body = self.posts()[0][3]
        self.assertEqual(body["proof"], checkin.build_proof(body["nonce"]))

    def test_later_slots_and_anyscore(self) -> None:
        self.iqi_score = 95
        self.assertEqual(checkin.run(at(0, 31)), 0)
        self.assertEqual(self.posts(), [])
        self.calls.clear()
        self.iqi_score = 100
        self.assertEqual(checkin.run(at(1, 1)), 0)
        self.assertEqual(self.posts(), [])
        self.calls.clear()
        self.iqi_score = 99
        self.after_checkin = 2
        self.assertEqual(checkin.run(at(1, 31)), 0)
        self.assertEqual(len(self.posts()), 1)

    def test_iqi_failure_waits_until_0131_fallback(self) -> None:
        self.iqi_error = checkin.ApiError("network", retryable=True)
        self.assertEqual(checkin.run(at(1, 1)), 0)
        self.assertEqual(self.posts(), [])
        self.assertIn("等待下一档", self.log_text())
        self.calls.clear()
        self.assertEqual(checkin.run(at(1, 31)), 0)
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("兜底签到", self.log_text())

    def test_iqi_auth_failure_does_not_fallback(self) -> None:
        self.iqi_error = checkin.ApiError("http-auth-rejected", retryable=False, http_status=401)
        self.assertEqual(checkin.run(at(1, 31)), 1)
        self.assertEqual(self.posts(), [])

    def test_low_reward_is_logged_once(self) -> None:
        self.checkin_message = "Checkin! Got 1 Points"
        self.after_checkin = 1
        self.assertEqual(checkin.run(at(0, 10)), 0)
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("低奖励异常", self.log_text())
        self.assertIn("不重复签到", self.log_text())
        self.assertNotIn("奖励已恢复", self.log_text())
        self.assertNotIn("积分已恢复", self.log_text())

    def lock_line(self, now: datetime) -> str:
        with redirect_stdout(io.StringIO()) as buf:
            checkin.run(now)
        lines = [line for line in buf.getvalue().splitlines() if line.startswith("GLADOS_DAY_LOCK=")]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_day_lock_stays_open_when_index_misses(self) -> None:
        self.iqi_score = 90
        self.assertEqual(self.lock_line(at(0, 1)), "GLADOS_DAY_LOCK=open")
        self.assertEqual(self.posts(), [])

    def test_day_lock_acquired_after_success_or_existing_history(self) -> None:
        self.after_checkin = 9
        self.assertEqual(self.lock_line(at(0, 1)), "GLADOS_DAY_LOCK=acquired")
        self.calls.clear()
        self.points_change = 9
        self.after_checkin = None
        self.assertEqual(self.lock_line(at(0, 31)), "GLADOS_DAY_LOCK=acquired")
        self.assertEqual(self.posts(), [])

    def test_local_lock_skips_later_slots_and_resets_next_day(self) -> None:
        self.after_checkin = 9
        self.assertEqual(checkin.run(at(0, 1)), 0)
        self.assertTrue(checkin.local_day_locked(at(0, 31)))
        self.calls.clear()
        self.assertEqual(checkin.run(at(0, 31)), 0)
        self.assertEqual(self.calls, [])
        self.assertIn("跳过 GLaDOS API", self.log_text())
        os.environ["GLADOS_FORCE"] = "1"
        self.assertEqual(checkin.run(at(1, 1)), 0)
        self.assertTrue(self.calls)
        os.environ["GLADOS_FORCE"] = ""
        self.calls.clear()
        next_day = at(0, 1).replace(day=10)
        self.assertFalse(checkin.local_day_locked(next_day))
        self.assertEqual(checkin.run(next_day), 0)
        self.assertTrue(self.calls)

    def test_already_checked_in_does_not_post(self) -> None:
        self.points_change = 9
        self.assertEqual(checkin.run(at(1, 31)), 0)
        self.assertEqual(self.posts(), [])
        self.assertIn("今日已签到", self.log_text())
        self.assertNotIn(NONCE, self.log_text())

    def test_retry_after_failed_class_uses_new_challenge(self) -> None:
        self.checkin_code = 2
        self.checkin_message = "temporary"
        self.assertEqual(checkin.run(at(0, 1)), 1)
        self.assertEqual(len(self.posts()), 3)
        nonces = [call[3]["nonce"] for call in self.posts()]
        self.assertNotEqual(nonces[0], nonces[1])
        self.assertNotEqual(
            checkin.build_proof(nonces[0]),
            checkin.build_proof(nonces[1]),
        )
        self.assertTrue(all("proof" in call[3] and "token" in call[3] for call in self.posts()))

    def test_logs_do_not_contain_credentials(self) -> None:
        self.after_checkin = 9
        checkin.run(at(0, 1))
        text = self.log_text()
        self.assertNotIn("synthetic-private", text)
        self.assertNotIn(NONCE, text)
        self.assertNotIn(PROOF, text)
        self.assertNotIn("hidden@example.test", text)
        self.assertNotIn(UA, text)


class ClassificationTests(unittest.TestCase):
    def test_result_classes(self) -> None:
        cases = [
            ({"code": 0, "message": "Checkin! Got 9 Points"}, "success"),
            ({"code": 1, "message": "Today's observation logged. Return tomorrow for more points."}, "already"),
            ({"code": 1, "message": "Checkin Repeats! Please try tomorrow"}, "already"),
            ({"code": -2, "message": "没有权限"}, "auth"),
            ({"code": 0, "message": "automated check-in detected"}, "auth"),
            ({"http_status": 403, "reason": "http-auth-rejected"}, "auth"),
            ({"request_failed": True, "reason": "checkin-challenge-unavailable"}, "challenge-unavailable"),
            ({"request_failed": True, "reason": "checkin-challenge-expired"}, "challenge-expired"),
            ({"code": 2, "message": "nope"}, "failed"),
        ]
        for payload, expected in cases:
            self.assertEqual(checkin.classify_checkin_result(payload), expected)

    def test_points_history_uses_business_and_epoch(self) -> None:
        moment = at(19)
        epoch_ms = int(moment.timestamp() * 1000)
        payload = {
            "code": 0,
            "points": "253.0",
            "history": [
                {
                    "time": epoch_ms,
                    "change": "1.0",
                    "business": "system:checkin",
                    "detail": "2026-11-09",
                    "asset": "points",
                }
            ],
        }
        self.assertTrue(checkin.already_checked_in_today(payload, moment))
        self.assertEqual(checkin.today_positive_change(payload, moment), 1.0)
        self.assertEqual(checkin.assess_reward(checkin.today_positive_change(payload, moment), None), "low")

    def test_reward_assessment(self) -> None:
        self.assertEqual(checkin.assess_reward(1, 9), "low")
        self.assertEqual(checkin.assess_reward(9, None), "ok")
        self.assertEqual(checkin.assess_reward(None, None), "unverified")


class CompatibilityTests(unittest.TestCase):
    def test_cookie_pairs(self) -> None:
        self.assertEqual(checkin.session_cookie_kind(COOKIE), "gld")
        self.assertEqual(checkin.session_cookie_kind(LEGACY_COOKIE), "koa")
        self.assertIsNone(checkin.session_cookie_kind("gld:sess=only-session"))
        self.assertIsNone(checkin.session_cookie_kind(""))

    def test_user_agent_normalization_and_default(self) -> None:
        self.assertEqual(checkin.normalize_user_agent(UA), (UA, False))
        self.assertEqual(checkin.normalize_user_agent('User-Agent: "' + UA + '"'), (UA, True))
        with self.assertRaises(checkin.UserAgentConfigurationError) as caught:
            checkin.normalize_user_agent(UA + "\nBroken")
        self.assertNotIn(UA, str(caught.exception))
        with mock.patch.dict(os.environ, {"GLADOS_USER_AGENT": ""}, clear=False):
            self.assertEqual(checkin.resolve_user_agent(), checkin.DEFAULT_USER_AGENT)

    def test_missing_cookie_stops_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "GLADOS_COOKIE": "",
                "GLADOS_LOG_PATH": os.path.join(tmp, "checkin.log"),
                "GLADOS_LOCK_DIR": os.path.join(tmp, "lock"),
                "GLADOS_FORCE": "",
            },
            clear=False,
        ), mock.patch("checkin.load_dotenv"), mock.patch("checkin.api") as api:
            self.assertEqual(checkin.run(at(16)), 2)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
