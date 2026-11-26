"""Algorithm tests: rounding invariants, optimality of the kept prefix."""

from __future__ import annotations

import random

import pytest

from app.algorithm import (
    AlgorithmError,
    build_correction_plan,
    build_initial_archive,
    independent_interval_check,
    max_unchanged_prefix,
    verify_archive,
)


def brute_force_max_prefix(old, new):
    """Reference implementation using the O(n^2) interval check."""
    n = len(old)
    best = 0
    for p in range(n + 1):
        ok = True
        for start in range(p):
            running = 0
            for finish in range(start, p):
                running += old[finish] * 1000 - new[finish]
                if abs(running) >= 1000:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            best = p
        else:
            break
    return best


def test_initial_archive_satisfies_every_interval():
    rng = random.Random(11)
    for n in (1, 2, 7, 61, 500, 1440):
        ml = [rng.randrange(0, 20000) for _ in range(n)]
        litres = build_initial_archive(ml)
        result = verify_archive(ml, litres)
        assert result.ok
        assert result.max_interval_error_ml == independent_interval_check(ml, litres)
        assert result.max_interval_error_ml < 1000
        assert all(v >= 0 for v in litres)
        assert abs(sum(litres) * 1000 - sum(ml)) < 1000


def test_initial_archive_exact_litres_are_unchanged():
    ml = [0, 1000, 2000, 5000]
    assert build_initial_archive(ml) == [0, 1, 2, 5]


@pytest.mark.parametrize("ml", [[], [-1], [1000, -5]])
def test_initial_archive_rejects_invalid(ml):
    with pytest.raises(AlgorithmError):
        build_initial_archive(ml)


def test_max_unchanged_prefix_matches_brute_force():
    rng = random.Random(3)
    for _ in range(120):
        n = rng.randrange(1, 40)
        old = build_initial_archive([rng.randrange(0, 4000) for _ in range(n)])
        new = [rng.randrange(0, 4000) for _ in range(n)]
        assert max_unchanged_prefix(old, new) == brute_force_max_prefix(old, new)


def test_max_unchanged_prefix_is_maximal():
    old = build_initial_archive([1000] * 20)
    new = [1000] * 20
    assert max_unchanged_prefix(old, new) == 20
    new[7] = 2600  # 1600 ml worse than the old whole litre at that minute
    assert max_unchanged_prefix(old, new) == 7


def test_max_unchanged_prefix_respects_error_budget():
    old = [0] * 10
    new = [0, 0, 600, 0, 0, 0, 0, 0, 0, 0]
    assert max_unchanged_prefix(old, new) == 10  # 600 ml is allowed
    new = [500, 0, 600, 0, 0, 0, 0, 0, 0, 0]
    # Prefix through minute 2 has cumulative -500 - 600 = -1100 -> only p=2.
    assert max_unchanged_prefix(old, new) == 2


def test_correction_plan_keeps_prefix_and_verifies():
    rng = random.Random(5)
    for _ in range(80):
        n = rng.randrange(1, 50)
        old_ml = [rng.randrange(0, 6000) for _ in range(n)]
        old = build_initial_archive(old_ml)
        new_ml = list(old_ml)
        for idx in range(n):
            if rng.random() < 0.35:
                new_ml[idx] = rng.randrange(0, 6000)
        plan = build_correction_plan(old, new_ml, list(range(n)))
        assert plan.new_archive[: plan.p_index] == tuple(old[: plan.p_index])
        assert plan.p_index == brute_force_max_prefix(old, new_ml)
        assert plan.verification.ok
        assert independent_interval_check(new_ml, plan.new_archive) < 1000
        assert plan.new_archive[plan.p_index :] == plan.suffix


def test_correction_plan_no_change_reports_full_keep():
    ml = [100, 250, 899, 0, 1200] * 6
    old = build_initial_archive(ml)
    plan = build_correction_plan(old, ml, list(range(30)))
    assert plan.p_index == 30
    assert plan.p_minute is None
    assert plan.new_archive == tuple(old)


def test_correction_plan_must_keep_prefix_when_old_was_bad_for_new_values():
    old = [0, 1, 0]
    new_ml = [0, 2500, 0]
    plan = build_correction_plan(old, new_ml, [0, 1, 2])
    assert plan.p_index == 1
    assert plan.new_archive == (0, 2, 0) or plan.new_archive[0] == 0
    assert independent_interval_check(new_ml, plan.new_archive) < 1000


def test_verify_rejects_bad_archives():
    assert not verify_archive([1000, 1000], [0, 0]).ok
    assert not verify_archive([0, 0], [1, 1]).ok
    assert not verify_archive([500, 500], [1, 1]).ok  # single-minute 500 each is fine, interval isn't
    assert verify_archive([500, 500], [0, 1]).ok
    assert verify_archive([500, 499], [0, 1]).ok
    assert not verify_archive([1000], [0]).ok
    assert not verify_archive([0], [1]).ok
    # Interval with 1000 exactly is rejected.
    assert not verify_archive([0, 600, 400], [0, 1, 1]).ok


def test_verify_detects_worst_interval_location():
    ml = [0, 400, 400, 0]
    litres = [0, 1, 1, 0]  # errors 0, +600, +600, 0 -> interval [0,2] = 1200
    result = verify_archive(ml, litres)
    assert not result.ok
    assert result.max_interval_error_ml == 1200
    assert (result.worst_start_index, result.worst_end_index) == (0, 2)
    assert independent_interval_check(ml, litres) == 1200


def test_single_huge_minute_value_still_works():
    ml = [0, 10**9, 0]
    litres = build_initial_archive(ml)
    assert verify_archive(ml, litres).ok
