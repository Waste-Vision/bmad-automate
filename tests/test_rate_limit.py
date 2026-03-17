"""Tests for rate_limit.py — RateLimiter."""

from __future__ import annotations

from bmad_automate.rate_limit import RateLimiter, is_rate_limited


class TestIsRateLimited:
    def test_detects_429(self):
        assert is_rate_limited("Error: HTTP 429 Too Many Requests")

    def test_detects_rate_limit_text(self):
        assert is_rate_limited("rate limit exceeded")

    def test_detects_too_many_requests(self):
        assert is_rate_limited("too many requests, please try again")

    def test_detects_throttle(self):
        assert is_rate_limited("request throttled")

    def test_normal_error_not_detected(self):
        assert not is_rate_limited("Connection refused")
        assert not is_rate_limited("File not found")

    def test_empty_string(self):
        assert not is_rate_limited("")


class TestRateLimiter:
    def test_acquire_release(self):
        rl = RateLimiter(max_concurrent=2)
        assert rl.acquire(timeout=0.01) is True
        rl.release()

    def test_semaphore_limit(self):
        rl = RateLimiter(max_concurrent=1)
        assert rl.acquire(timeout=0.01) is True
        # Second acquire should fail (timeout)
        assert rl.acquire(timeout=0.01) is False
        rl.release()

    def test_backoff_exponential(self):
        rl = RateLimiter(initial_backoff=10.0, backoff_factor=2.0, max_backoff=100.0)

        b1 = rl.record_rate_limit(1)
        assert b1 == 10.0

        b2 = rl.record_rate_limit(1)
        assert b2 == 20.0

        b3 = rl.record_rate_limit(1)
        assert b3 == 40.0

    def test_backoff_capped(self):
        rl = RateLimiter(initial_backoff=100.0, max_backoff=200.0, backoff_factor=3.0)
        rl.record_rate_limit(1)
        b2 = rl.record_rate_limit(1)
        assert b2 == 200.0  # capped at max

    def test_success_resets_backoff(self):
        rl = RateLimiter(initial_backoff=10.0)
        rl.record_rate_limit(1)
        rl.record_rate_limit(1)
        rl.record_success(1)
        assert rl.get_backoff(1) == 0.0

    def test_independent_epics(self):
        rl = RateLimiter(initial_backoff=10.0)
        rl.record_rate_limit(1)
        assert rl.get_backoff(1) == 10.0
        assert rl.get_backoff(2) == 0.0

    def test_should_degrade(self):
        rl = RateLimiter(initial_backoff=1.0)
        for _ in range(5):
            rl.record_rate_limit(1)
        assert rl.should_degrade_to_sequential(1) is True
        assert rl.should_degrade_to_sequential(2) is False

    def test_adjust_concurrency(self):
        rl = RateLimiter(max_concurrent=3)
        rl.adjust_concurrency(1)
        assert rl.max_concurrent == 1

    def test_adjust_concurrency_minimum_one(self):
        rl = RateLimiter(max_concurrent=3)
        rl.adjust_concurrency(0)
        assert rl.max_concurrent == 1
