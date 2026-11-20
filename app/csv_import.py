"""CSV 解析与基础校验。"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Sequence

from . import matching


class CsvError(ValueError):
    """表内容不合法，需要退回。"""


@dataclass(frozen=True)
class ParsedTable:
    filename: str
    minute_base: int
    duration: int
    joins: tuple[int, ...]
    boards: tuple[int, ...]
    filled_minutes: tuple[int, ...] = ()

    @property
    def total_passengers(self) -> int:
        return sum(self.joins)

    @property
    def last_minute(self) -> int:
        return self.minute_base + self.duration - 1


MINUTE_NAMES = {
    "minute",
    "minutes",
    "min",
    "mins",
    "time",
    "分钟",
    "分钟号",
    "时间",
    "时刻",
    "第几分钟",
}
JOIN_NAMES = {
    "join",
    "joins",
    "join_count",
    "joined",
    "arrivals",
    "arrive",
    "arrived",
    "入队",
    "入队人数",
    "到达",
    "到达人数",
}
BOARD_NAMES = {
    "board",
    "boards",
    "board_count",
    "boarded",
    "boarding",
    "depart",
    "departed",
    "登车",
    "登车人数",
    "上车",
    "上车人数",
}


def _normalize_header(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", name.strip().lower())


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("无法识别 CSV 编码，请另存为 UTF-8")


def _parse_int(raw: str, line_no: int, what: str) -> int:
    text = raw.strip()
    if not text:
        raise CsvError(f"第 {line_no} 行{what}为空")
    if not re.fullmatch(r"[+-]?\d+", text):
        raise CsvError(f"第 {line_no} 行{what}不是整数：{raw!r}")
    return int(text)


def _header_positions(row: Sequence[str]) -> dict[str, int] | None:
    normalized = [_normalize_header(cell) for cell in row]
    positions: dict[str, int] = {}
    for index, name in enumerate(normalized):
        if name in MINUTE_NAMES and "minute" not in positions:
            positions["minute"] = index
        elif name in JOIN_NAMES and "joins" not in positions:
            positions["joins"] = index
        elif name in BOARD_NAMES and "boards" not in positions:
            positions["boards"] = index
    if len(positions) == 3 and len(set(positions.values())) == 3:
        return positions
    return None


def parse_csv(data: bytes, filename: str = "upload.csv") -> ParsedTable:
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[int, list[str]]] = []
    for line_no, row in enumerate(reader, start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        rows.append((line_no, row))
    if not rows:
        raise CsvError("CSV 为空，没有可导入的分钟记录")
    if len(rows) > matching.MAX_MINUTE + 1:
        raise CsvError(f"单场至多 {matching.MAX_MINUTE} 分钟，表中有 {len(rows)} 行")

    column_order = (0, 1, 2)
    _, first_row = rows[0]
    looks_numeric = all(re.fullmatch(r"[+-]?\d+", cell.strip()) for cell in first_row[:1]) and len(first_row) >= 3
    if not looks_numeric:
        positions = _header_positions(first_row)
        if positions is None:
            raise CsvError(
                "无法识别表头，请使用“分钟号, 入队人数, 登车人数”"
                "（或 minute, joins, boards）"
            )
        column_order = (positions["minute"], positions["joins"], positions["boards"])
        rows = rows[1:]
        if not rows:
            raise CsvError("CSV 只有表头，没有分钟记录")

    minute_counts: dict[int, tuple[int, int]] = {}
    for line_no, row in rows:
        cells = list(row)
        while cells and not cells[-1].strip():
            cells.pop()
        if len(cells) != 3:
            raise CsvError(f"第 {line_no} 行应有 3 列（分钟号, 入队人数, 登车人数），实际 {len(cells)} 列")
        minute_raw = cells[column_order[0]]
        join_raw = cells[column_order[1]]
        board_raw = cells[column_order[2]]
        minute = _parse_int(minute_raw, line_no, "分钟号")
        joined = _parse_int(join_raw, line_no, "入队人数")
        boarded = _parse_int(board_raw, line_no, "登车人数")
        if minute < 0:
            raise CsvError(f"第 {line_no} 行分钟号不能为负：{minute}")
        if joined < 0 or boarded < 0:
            raise CsvError(f"第 {line_no} 行人数不能为负")
        if minute in minute_counts:
            raise CsvError(f"第 {line_no} 行分钟号 {minute} 重复")
        minute_counts[minute] = (joined, boarded)

    minutes = sorted(minute_counts)
    base = minutes[0]
    duration = minutes[-1] - base + 1
    if duration > matching.MAX_MINUTE:
        raise CsvError(f"单场至多 {matching.MAX_MINUTE} 分钟，表中跨度 {duration} 分钟")
    # 未出现的分钟按“该分钟无入队、无登车”补齐，并在导入结果中明确提示。
    filled = tuple(m for m in range(base, minutes[-1] + 1) if m not in minute_counts)
    joins = [minute_counts.get(m, (0, 0))[0] for m in range(base, minutes[-1] + 1)]
    boards = [minute_counts.get(m, (0, 0))[1] for m in range(base, minutes[-1] + 1)]

    try:
        matching.check_feasible(joins, boards)
    except matching.InfeasibleError as exc:
        raise CsvError(str(exc)) from exc

    return ParsedTable(
        filename=filename,
        minute_base=base,
        duration=duration,
        joins=tuple(joins),
        boards=tuple(boards),
        filled_minutes=filled,
    )
