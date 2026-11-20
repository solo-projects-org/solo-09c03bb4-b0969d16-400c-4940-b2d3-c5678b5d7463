"""上下界算法的穷举交叉验证。"""

from __future__ import annotations

import itertools
import random

import pytest

from app import matching


def brute_force_bounds(joins, boards, threshold):
    """把乘客和登车名额都展开成个体，穷举全排列求上下界。"""
    arrivals = [m for m, n in enumerate(joins) for _ in range(n)]
    slots = [m for m, n in enumerate(boards) for _ in range(n)]
    if len(arrivals) != len(slots):
        return None
    best_low = None
    best_high = None
    for perm in itertools.permutations(range(len(slots))):
        overdue = 0
        for i, slot_index in enumerate(perm):
            board = slots[slot_index]
            if board < arrivals[i]:
                break
            if board - arrivals[i] > threshold:
                overdue += 1
        else:
            best_low = overdue if best_low is None else min(best_low, overdue)
            best_high = overdue if best_high is None else max(best_high, overdue)
    if best_low is None:
        return None
    return best_low, best_high


def sparse(counts: dict[int, int], size: int = 7) -> list[int]:
    result = [0] * size
    for minute, value in counts.items():
        result[minute] = value
    return result


@pytest.mark.parametrize("threshold", [0, 1, 2, 3, 5, 15])
def test_greedy_matches_brute_force(threshold):
    random.seed(20241120 + threshold)
    checked = 0
    for _ in range(250):
        n = random.randint(1, 6)
        joins = sparse({}, 7)
        boards = sparse({}, 7)
        arrivals = sorted(random.randint(0, 6) for _ in range(n))
        for arrival in arrivals:
            joins[arrival] += 1
        slots = sorted(random.randint(a, 6) for a in arrivals)
        random.shuffle(slots)
        slots.sort()
        for slot in slots:
            boards[slot] += 1
        expected = brute_force_bounds(joins, boards, threshold)
        if expected is None:
            with pytest.raises(matching.InfeasibleError):
                matching.check_feasible(joins, boards)
            continue
        matching.check_feasible(joins, boards)
        got = (
            matching.lower_bound(joins, boards, threshold),
            matching.upper_bound(joins, boards, threshold),
        )
        assert got == expected, (joins, boards, threshold, expected, got)
        checked += 1
    assert checked > 100


def test_bounds_are_non_negative_justified_values():
    joins = sparse({0: 3, 1: 1, 4: 2})
    boards = sparse({2: 2, 5: 2, 6: 2})
    matching.check_feasible(joins, boards)
    bounds = matching.compute_bounds(joins, boards, 2)
    assert bounds.lower <= bounds.upper
    assert bounds.total == 6
    assert matching.classify(bounds.lower, bounds.upper, 10) == matching.STATUS_MEETS


def test_infeasible_when_board_exceeds_join():
    joins = sparse({0: 1})
    boards = sparse({0: 1, 1: 1})
    with pytest.raises(matching.InfeasibleError):
        matching.check_feasible(joins, boards)


def test_infeasible_when_boarding_before_arrival():
    joins = sparse({3: 2})
    boards = sparse({2: 1, 3: 1})
    with pytest.raises(matching.InfeasibleError):
        matching.check_feasible(joins, boards)


def test_queue_must_end_empty():
    joins = sparse({0: 3})
    boards = sparse({0: 2})
    with pytest.raises(matching.InfeasibleError):
        matching.check_feasible(joins, boards)


def test_classify_boundaries():
    assert matching.classify(0, 10, 10) == matching.STATUS_MEETS
    assert matching.classify(11, 12, 10) == matching.STATUS_EXCEEDS
    assert matching.classify(10, 11, 10) == matching.STATUS_REVIEW
    assert matching.classify(3, 7, 10) == matching.STATUS_MEETS


def test_greedy_bounds_too_large_scale_is_fast():
    joins = [40] * matching.MAX_MINUTE
    boards = [40] * matching.MAX_MINUTE
    matching.check_feasible(joins, boards)
    bounds = matching.compute_bounds(joins, boards, 15)
    assert bounds.total == 40 * matching.MAX_MINUTE
    # 每分钟入队与登车人数相同，人人都能当分钟登车
    assert bounds.lower == 0
    assert bounds.upper == 0


def test_upper_bound_can_reach_full_delay():
    # 第 0 分钟入队 40 人，之后每分钟登 2 人；总有人要等超过 15 分钟
    joins = [40] + [0] * 19
    boards = [2] * 20
    matching.check_feasible(joins, boards)
    bounds = matching.compute_bounds(joins, boards, 15)
    # 第 0..15 分钟各接走 2 人（等待未超过 15 分钟），剩下 8 人只能等到第 16 分钟及以后
    assert bounds.lower == 8
    assert bounds.upper == 8
