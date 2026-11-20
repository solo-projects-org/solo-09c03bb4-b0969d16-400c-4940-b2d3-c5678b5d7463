"""新闸机读头表：解析、方向分法枚举与分钟表换算。

新闸机按读头导出计数，每行是（分钟号, 读头号, 人数），本身不带入队/登车
方向。每个读头的方向固定。用一场已人工核对的场次做对照：某种方向分法
（每个读头划归入队或登车）若能逐分钟还原该场次的入队和登车人数，就是
一个可行分法。现行对照的全部可行分法若把新读头表换算成同一张分钟表，
新场次的入队/登车人数才被唯一确定。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Sequence

from . import matching
from .csv_import import MINUTE_NAMES, CsvError, _decode, _parse_int

MAX_READERS = 12

JOIN = "J"  # 入队方向
BOARD = "B"  # 登车方向


@dataclass(frozen=True)
class ReaderTable:
    """一张读头表：分钟范围、读头集合和每个（分钟, 读头）的人数。"""

    filename: str
    minute_base: int
    duration: int
    readers: tuple[int, ...]  # 升序读头号
    counts: tuple[tuple[int, ...], ...]  # counts[分钟偏移][读头下标]
    filled_minutes: tuple[int, ...] = ()

    @property
    def last_minute(self) -> int:
        return self.minute_base + self.duration - 1

    @property
    def total_passengers(self) -> int:
        return sum(sum(row) for row in self.counts)


READER_NAMES = {
    "reader",
    "readerid",
    "readerno",
    "head",
    "headid",
    "gate",
    "gateid",
    "读头",
    "读头号",
    "读头编号",
    "闸机",
    "闸机号",
    "闸机读头",
}
COUNT_NAMES = {
    "count",
    "counts",
    "people",
    "passengers",
    "amount",
    "人数",
    "计数",
    "数量",
    "人流量",
}


def _normalize_header(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", name.strip().lower())


def _header_positions(row: Sequence[str]) -> dict[str, int] | None:
    normalized = [_normalize_header(cell) for cell in row]
    positions: dict[str, int] = {}
    for index, name in enumerate(normalized):
        if name in MINUTE_NAMES and "minute" not in positions:
            positions["minute"] = index
        elif name in READER_NAMES and "reader" not in positions:
            positions["reader"] = index
        elif name in COUNT_NAMES and "count" not in positions:
            positions["count"] = index
    if len(positions) == 3 and len(set(positions.values())) == 3:
        return positions
    return None


def parse_reader_csv(data: bytes, filename: str = "upload.csv") -> ReaderTable:
    """解析读头 CSV：每行（分钟号, 读头号, 人数），缺行按 0 人计。"""
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[int, list[str]]] = []
    for line_no, row in enumerate(reader, start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        rows.append((line_no, row))
    if not rows:
        raise CsvError("CSV 为空，没有可导入的读头记录")
    if len(rows) > matching.MAX_MINUTE * MAX_READERS + 1:
        raise CsvError(
            f"单场至多 {matching.MAX_MINUTE} 分钟、{MAX_READERS} 个读头，"
            f"表中有 {len(rows)} 行"
        )

    column_order = (0, 1, 2)
    _, first_row = rows[0]
    looks_numeric = all(
        re.fullmatch(r"[+-]?\d+", cell.strip()) for cell in first_row[:1]
    ) and len(first_row) >= 3
    if not looks_numeric:
        positions = _header_positions(first_row)
        if positions is None:
            raise CsvError(
                "无法识别表头，请使用“分钟号, 读头号, 人数”"
                "（或 minute, reader, count）"
            )
        column_order = (positions["minute"], positions["reader"], positions["count"])
        rows = rows[1:]
        if not rows:
            raise CsvError("CSV 只有表头，没有读头记录")

    cell_counts: dict[tuple[int, int], int] = {}
    for line_no, row in rows:
        cells = list(row)
        while cells and not cells[-1].strip():
            cells.pop()
        if len(cells) != 3:
            raise CsvError(
                f"第 {line_no} 行应有 3 列（分钟号, 读头号, 人数），实际 {len(cells)} 列"
            )
        minute = _parse_int(cells[column_order[0]], line_no, "分钟号")
        head = _parse_int(cells[column_order[1]], line_no, "读头号")
        count = _parse_int(cells[column_order[2]], line_no, "人数")
        if minute < 0:
            raise CsvError(f"第 {line_no} 行分钟号不能为负：{minute}")
        if head < 0:
            raise CsvError(f"第 {line_no} 行读头号不能为负：{head}")
        if count < 0:
            raise CsvError(f"第 {line_no} 行人数不能为负")
        key = (minute, head)
        if key in cell_counts:
            raise CsvError(f"第 {line_no} 行（分钟 {minute}, 读头 {head}）重复")
        cell_counts[key] = count

    heads = sorted({head for _, head in cell_counts})
    if len(heads) > MAX_READERS:
        raise CsvError(f"同一套读头至多 {MAX_READERS} 个，表中有 {len(heads)} 个")

    minutes = sorted({minute for minute, _ in cell_counts})
    base = minutes[0]
    duration = minutes[-1] - base + 1
    if duration > matching.MAX_MINUTE:
        raise CsvError(
            f"单场至多 {matching.MAX_MINUTE} 分钟，表中跨度 {duration} 分钟"
        )

    head_index = {head: i for i, head in enumerate(heads)}
    counts = tuple(
        tuple(
            cell_counts.get((minute, head), 0) for head in heads
        )
        for minute in range(base, minutes[-1] + 1)
    )
    filled = tuple(m for m in range(base, minutes[-1] + 1) if not any(counts[m - base]))
    return ReaderTable(
        filename=filename,
        minute_base=base,
        duration=duration,
        readers=tuple(heads),
        counts=counts,
        filled_minutes=filled,
    )


def align_to_range(table: ReaderTable, base: int, duration: int) -> ReaderTable:
    """把读头表零填充到指定分钟范围（调用方保证表的分钟号都在范围内）。"""
    if not (base <= table.minute_base and table.last_minute < base + duration):
        raise ValueError("读头表分钟号超出目标范围，无法对齐")
    offset = table.minute_base - base
    zero = tuple(0 for _ in table.readers)
    counts = tuple(
        table.counts[t - offset] if offset <= t < offset + table.duration else zero
        for t in range(duration)
    )
    return ReaderTable(
        filename=table.filename,
        minute_base=base,
        duration=duration,
        readers=table.readers,
        counts=counts,
    )


def minute_total_mismatch(
    table: ReaderTable, joins: Sequence[int], boards: Sequence[int]
) -> int | None:
    """返回第一个“读头合计 ≠ 入队+登车”的分钟偏移；都一致返回 None。"""
    for t in range(table.duration):
        if sum(table.counts[t]) != joins[t] + boards[t]:
            return t
    return None


def _mask_to_assignment(mask: int, size: int) -> str:
    return "".join(JOIN if mask >> i & 1 else BOARD for i in range(size))


def find_assignments(
    table: ReaderTable, joins: Sequence[int], boards: Sequence[int]
) -> list[str]:
    """枚举所有可行分法：逐分钟还原 joins/boards 的读头方向划分。

    返回的每个分法是长度等于读头数的字符串，按 ``table.readers`` 顺序，
    ``"J"`` 表示该读头计入队，``"B"`` 表示计登车。调用前要求每分钟读头
    合计与 joins+boards 一致（否则直接判无解）。
    """
    if minute_total_mismatch(table, joins, boards) is not None:
        return []
    size = len(table.readers)
    full = 1 << size
    candidates: list[int] | None = None
    for t in range(table.duration):
        target = joins[t]
        row = table.counts[t]
        # 该分钟所有读头子集的入队侧人数（低位读头增量 DP）
        subset_sum = [0] * full
        for mask in range(1, full):
            low = mask & (-mask)
            subset_sum[mask] = subset_sum[mask ^ low] + row[low.bit_length() - 1]
        if candidates is None:
            candidates = [mask for mask in range(full) if subset_sum[mask] == target]
        else:
            candidates = [mask for mask in candidates if subset_sum[mask] == target]
        if not candidates:
            return []
    return [_mask_to_assignment(mask, size) for mask in sorted(candidates or [])]


def convert_with_assignment(
    table: ReaderTable, readers: Sequence[int], assignment: str
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """按给定分法把读头表换算成（入队分钟表, 登车分钟表）。

    ``readers``/``assignment`` 描述对照中每个读头号的方向；表中的读头必须
    都在 ``readers`` 里（调用方保证）。
    """
    direction = dict(zip(readers, assignment))
    joins = [0] * table.duration
    boards = [0] * table.duration
    for index, head in enumerate(table.readers):
        target = joins if direction[head] == JOIN else boards
        for t in range(table.duration):
            target[t] += table.counts[t][index]
    return tuple(joins), tuple(boards)


def unique_minute_table(
    table: ReaderTable, readers: Sequence[int], assignments: Sequence[str]
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """各可行分法换算出的分钟表完全相同时返回该表，否则返回 None。"""
    result: tuple[tuple[int, ...], tuple[int, ...]] | None = None
    for assignment in assignments:
        converted = convert_with_assignment(table, readers, assignment)
        if result is None:
            result = converted
        elif converted != result:
            return None
    return result
