"""等待时长上下界计算。

数据模型
--------
一场运营只用每分钟的入队人数 ``joins[t]`` 和登车人数 ``boards[t]`` 描述。
乘客不可区分，因此一次“完整配对”就是把每个登车名额分配给某个入队分钟
不晚于该登车分钟的乘客。所有乘客最终都能登车（散场队列为空）时，完整配对
才存在。

对任意完整配对，等待超过阈值的乘客数有一个最小值和一个最大值。本模块用
两个贪心过程直接求出这两个界：

* 最小超时数：按登车分钟升序处理。优先用等待尚未超时（入队分钟在
  ``[b - T, b]``）的乘客，且先用最早入队者；只有没有未超时乘客时才动用
  已超时的最早入队者。这样把“按时登车”的名额尽量留给最容易过期的人。
* 最大超时数：按登车分钟升序处理。只要有已超时乘客，就先用其中入队最晚
  的一个，保证本次一定超时；否则用未超时乘客中入队最晚的一个，把更早
  入队、更接近过期的乘客留到后面。

两个贪心都只按分钟聚合搬运人数，复杂度与乘客总数无关（分钟数最多 121）。
``tests/test_matching.py`` 用穷举全排列对它们做了交叉验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

MAX_MINUTE = 120
DEFAULT_THRESHOLD = 15
DEFAULT_LIMIT = 10

STATUS_MEETS = "meets"
STATUS_EXCEEDS = "exceeds"
STATUS_REVIEW = "review"

STATUS_LABELS = {
    STATUS_MEETS: "达标",
    STATUS_EXCEEDS: "超标",
    STATUS_REVIEW: "待核",
}


class InfeasibleError(ValueError):
    """整场无法配齐。"""


@dataclass(frozen=True)
class Bounds:
    lower: int
    upper: int
    total: int


def empty_counts(duration: int = MAX_MINUTE + 1) -> list[int]:
    return [0] * duration


def counts_from_pairs(pairs: Iterable[tuple[int, int]], duration: int) -> list[int]:
    counts = empty_counts(duration)
    for minute, amount in pairs:
        counts[minute] += amount
    return counts


def validate_structure(joins: Sequence[int], boards: Sequence[int]) -> None:
    """检查长度、非负性等基础结构。"""
    if len(joins) != len(boards):
        raise ValueError("入队与登车分钟序列长度不一致")
    if not joins:
        raise ValueError("分钟序列不能为空")
    if len(joins) > MAX_MINUTE:
        raise ValueError(f"单场至多 {MAX_MINUTE} 分钟")
    for minute, value in enumerate(joins):
        if value < 0:
            raise ValueError(f"第 {minute} 分钟入队人数为负数")
    for minute, value in enumerate(boards):
        if value < 0:
            raise ValueError(f"第 {minute} 分钟登车人数为负数")


def check_feasible(joins: Sequence[int], boards: Sequence[int]) -> None:
    """检查是否存在完整配对；不存在时抛出带中文原因的 InfeasibleError。

    条件等价于：任意前缀内登车人数不超过入队人数，且全场总数相等。
    """
    validate_structure(joins, boards)
    waiting = 0
    total_joins = 0
    total_boards = 0
    for minute, (joined, boarded) in enumerate(zip(joins, boards)):
        waiting += joined - boarded
        total_joins += joined
        total_boards += boarded
        if waiting < 0:
            raise InfeasibleError(
                f"第 {minute} 分钟登车人数超过此前累计入队人数，"
                "开场或期间队列不能为负"
            )
    if total_joins != total_boards:
        raise InfeasibleError(
            f"散场时队列不为空：全场入队 {total_joins} 人，登车 {total_boards} 人"
        )


def lower_bound(joins: Sequence[int], boards: Sequence[int], threshold: int = DEFAULT_THRESHOLD) -> int:
    """所有完整配对中超时人数的最小值。调用前须保证可配齐。"""
    check_feasible(joins, boards)
    remaining = list(joins)
    overdue = 0
    for boarded_minute, board_count in enumerate(boards):
        need = board_count
        if need == 0:
            continue
        window_start = max(0, boarded_minute - threshold)
        # 未超时乘客中，先安排最早入队者。
        for joined_minute in range(window_start, boarded_minute + 1):
            if need == 0:
                break
            take = min(need, remaining[joined_minute])
            if take:
                remaining[joined_minute] -= take
                need -= take
        # 没有未超时乘客了，只能安排已超时者，取最早入队者。
        for joined_minute in range(0, window_start):
            if need == 0:
                break
            take = min(need, remaining[joined_minute])
            if take:
                remaining[joined_minute] -= take
                need -= take
                overdue += take
        if need:
            raise InfeasibleError(f"第 {boarded_minute} 分钟没有可登车的乘客")
    if any(remaining):
        raise InfeasibleError("散场后仍有未登车乘客")
    return overdue


def upper_bound(joins: Sequence[int], boards: Sequence[int], threshold: int = DEFAULT_THRESHOLD) -> int:
    """所有完整配对中超时人数的最大值。调用前须保证可配齐。"""
    check_feasible(joins, boards)
    remaining = list(joins)
    overdue = 0
    for boarded_minute, board_count in enumerate(boards):
        need = board_count
        if need == 0:
            continue
        # 已超时乘客优先，尽量制造超时；取入队最晚者。
        for joined_minute in range(boarded_minute - threshold - 1, -1, -1):
            if need == 0:
                break
            take = min(need, remaining[joined_minute])
            if take:
                remaining[joined_minute] -= take
                need -= take
                overdue += take
        # 剩下的只能用未超时乘客，取入队最晚者，保留更早入队者给后续。
        for joined_minute in range(boarded_minute, max(-1, boarded_minute - threshold - 1), -1):
            if need == 0:
                break
            take = min(need, remaining[joined_minute])
            if take:
                remaining[joined_minute] -= take
                need -= take
        if need:
            raise InfeasibleError(f"第 {boarded_minute} 分钟没有可登车的乘客")
    if any(remaining):
        raise InfeasibleError("散场后仍有未登车乘客")
    return overdue


def compute_bounds(
    joins: Sequence[int],
    boards: Sequence[int],
    threshold: int = DEFAULT_THRESHOLD,
) -> Bounds:
    check_feasible(joins, boards)
    return Bounds(
        lower=lower_bound(joins, boards, threshold),
        upper=upper_bound(joins, boards, threshold),
        total=sum(joins),
    )


def classify(lower: int, upper: int, limit: int = DEFAULT_LIMIT) -> str:
    """按“超时人数是否多于 limit”判定。

    * 上界不超过 limit：无论怎样配对都不会超标 -> 达标
    * 下界已经超过 limit：无论怎样配对都超标 -> 超标
    * 其余情况：结论取决于具体配对 -> 待核
    """
    if upper <= limit:
        return STATUS_MEETS
    if lower > limit:
        return STATUS_EXCEEDS
    return STATUS_REVIEW


def status_label(status: str) -> str:
    return STATUS_LABELS[status]
