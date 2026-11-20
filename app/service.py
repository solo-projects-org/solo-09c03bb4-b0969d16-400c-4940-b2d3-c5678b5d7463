"""业务服务：场次、结论、凭证补录、读头对照与读头表导入。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from . import matching, readers
from .csv_import import ParsedTable
from .db import connect


class NotFoundError(LookupError):
    """场次不存在。"""


class CredentialConflictError(ValueError):
    """同一凭证号出现了不同内容。"""


class CredentialInfeasibleError(ValueError):
    """补录后整场无法配齐，或凭证与闸机记录矛盾。"""


class CalibrationError(ValueError):
    """新对照不合法或无解；原对照继续生效。"""


class PreviewError(ValueError):
    """读头表无法生成可导入的预览，整表退回。"""


class PreviewStaleError(ValueError):
    """确认时现行对照已无法复核出原预览，未建场。"""


@dataclass(frozen=True)
class SessionData:
    id: int
    filename: str
    minute_base: int
    duration: int
    joins: list[int]
    boards: list[int]
    total_passengers: int
    threshold: int
    limit_count: int
    source: str
    verified_at: str | None
    created_at: str

    @property
    def last_minute(self) -> int:
        return self.minute_base + self.duration - 1

    @property
    def minute_numbers(self) -> list[int]:
        return list(range(self.minute_base, self.last_minute + 1))

    @property
    def source_label(self) -> str:
        return "读头表" if self.source == "reader" else "方向 CSV"


@dataclass(frozen=True)
class ResultData:
    session_id: int
    lower: int
    upper: int
    status: str
    pinned_overdue: int
    residual_lower: int
    residual_upper: int
    version: int
    updated_at: str

    @property
    def status_label(self) -> str:
        return matching.status_label(self.status)


@dataclass(frozen=True)
class CredentialData:
    id: int
    session_id: int
    credential_no: str
    join_minute: int
    board_minute: int
    count: int
    created_at: str

    @property
    def wait_minutes(self) -> int:
        return self.board_minute - self.join_minute

    def is_overdue(self, threshold: int) -> bool:
        return self.wait_minutes > threshold


@dataclass(frozen=True)
class Outcome:
    outcome: str  # applied / duplicate
    session_id: int
    credential: CredentialData
    result: ResultData


@dataclass(frozen=True)
class CalibrationData:
    """一次读头方向对照；数据库中 id 最大的一行是现行对照。"""

    id: int
    session_id: int
    filename: str
    minute_base: int
    duration: int
    readers: tuple[int, ...]
    assignments: tuple[str, ...]
    counts: tuple[tuple[int, ...], ...]
    created_at: str


@dataclass(frozen=True)
class PreviewData:
    """一次读头表导入预览；session_id 非空表示已建场。"""

    id: int
    filename: str
    minute_base: int
    duration: int
    readers: tuple[int, ...]
    counts: tuple[tuple[int, ...], ...]
    joins: tuple[int, ...]
    boards: tuple[int, ...]
    calibration_id: int
    session_id: int | None
    created_at: str
    confirmed_at: str | None

    @property
    def last_minute(self) -> int:
        return self.minute_base + self.duration - 1

    @property
    def total_passengers(self) -> int:
        return sum(self.joins)

    def reader_table(self) -> readers.ReaderTable:
        return readers.ReaderTable(
            filename=self.filename,
            minute_base=self.minute_base,
            duration=self.duration,
            readers=self.readers,
            counts=self.counts,
        )


@dataclass(frozen=True)
class ConfirmOutcome:
    outcome: str  # created / existing
    preview_id: int
    session_id: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_from_row(row: sqlite3.Row) -> SessionData:
    return SessionData(
        id=row["id"],
        filename=row["filename"],
        minute_base=row["minute_base"],
        duration=row["duration"],
        joins=json.loads(row["joins_json"]),
        boards=json.loads(row["boards_json"]),
        total_passengers=row["total_passengers"],
        threshold=row["threshold"],
        limit_count=row["limit_count"],
        source=row["source"],
        verified_at=row["verified_at"],
        created_at=row["created_at"],
    )


def _result_from_row(row: sqlite3.Row) -> ResultData:
    return ResultData(
        session_id=row["session_id"],
        lower=row["lower"],
        upper=row["upper"],
        status=row["status"],
        pinned_overdue=row["pinned_overdue"],
        residual_lower=row["residual_lower"],
        residual_upper=row["residual_upper"],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _credential_from_row(row: sqlite3.Row) -> CredentialData:
    return CredentialData(
        id=row["id"],
        session_id=row["session_id"],
        credential_no=row["credential_no"],
        join_minute=row["join_minute"],
        board_minute=row["board_minute"],
        count=row["count"],
        created_at=row["created_at"],
    )


def _calibration_from_row(row: sqlite3.Row) -> CalibrationData:
    return CalibrationData(
        id=row["id"],
        session_id=row["session_id"],
        filename=row["filename"],
        minute_base=row["minute_base"],
        duration=row["duration"],
        readers=tuple(json.loads(row["readers_json"])),
        assignments=tuple(json.loads(row["assignments_json"])),
        counts=tuple(tuple(r) for r in json.loads(row["counts_json"])),
        created_at=row["created_at"],
    )


def _preview_from_row(row: sqlite3.Row) -> PreviewData:
    return PreviewData(
        id=row["id"],
        filename=row["filename"],
        minute_base=row["minute_base"],
        duration=row["duration"],
        readers=tuple(json.loads(row["readers_json"])),
        counts=tuple(tuple(r) for r in json.loads(row["counts_json"])),
        joins=tuple(json.loads(row["joins_json"])),
        boards=tuple(json.loads(row["boards_json"])),
        calibration_id=row["calibration_id"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        confirmed_at=row["confirmed_at"],
    )


def _load_session(conn: sqlite3.Connection, session_id: int) -> SessionData:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"场次 {session_id} 不存在")
    return _session_from_row(row)


def _load_result(conn: sqlite3.Connection, session_id: int) -> ResultData:
    row = conn.execute("SELECT * FROM results WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"场次 {session_id} 还没有结论")
    return _result_from_row(row)


def _load_credentials(conn: sqlite3.Connection, session_id: int) -> list[CredentialData]:
    rows = conn.execute(
        "SELECT * FROM credentials WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [_credential_from_row(row) for row in rows]


@dataclass(frozen=True)
class CredentialInput:
    credential_no: str
    join_minute: int
    board_minute: int
    count: int


def _compute(
    session: SessionData,
    credentials: Iterable[CredentialData | CredentialInput],
) -> tuple[int, int, int, int, int, str]:
    """在闸机总数上核销已采信凭证，再求剩余匿名乘客的上下界。

    凭证乘客本来就被计入闸机人数，因此按凭证的入队/登车分钟从匿名池中各扣掉
    相同人数：这部分乘客的等待时长已经确定，剩余不确定性只来自匿名乘客。
    """
    residual_joins = list(session.joins)
    residual_boards = list(session.boards)
    pinned_overdue = 0

    for record in credentials:
        join_index = record.join_minute - session.minute_base
        board_index = record.board_minute - session.minute_base
        count = record.count
        if not (0 <= join_index < session.duration and 0 <= board_index < session.duration):
            raise CredentialInfeasibleError(
                f"凭证 {record.credential_no} 的分钟号超出本场范围"
            )
        if board_index < join_index:
            raise CredentialInfeasibleError(
                f"凭证 {record.credential_no} 的登车分钟早于入队分钟"
            )
        residual_joins[join_index] -= count
        residual_boards[board_index] -= count
        if residual_joins[join_index] < 0:
            raise CredentialInfeasibleError(
                f"凭证 {record.credential_no} 的 {count} 人超过第 "
                f"{session.minute_base + join_index} 分钟记录的入队人数，"
                "凭证与闸机记录矛盾，无法核销"
            )
        if residual_boards[board_index] < 0:
            raise CredentialInfeasibleError(
                f"凭证 {record.credential_no} 的 {count} 人超过第 "
                f"{session.minute_base + board_index} 分钟记录的登车人数，"
                "凭证与闸机记录矛盾，无法核销"
            )
        if board_index - join_index > session.threshold:
            pinned_overdue += count

    try:
        matching.check_feasible(residual_joins, residual_boards)
        residual_lower = matching.lower_bound(residual_joins, residual_boards, session.threshold)
        residual_upper = matching.upper_bound(residual_joins, residual_boards, session.threshold)
    except matching.InfeasibleError as exc:
        raise CredentialInfeasibleError(
            f"核销凭证后剩余乘客无法配齐全场：{exc}"
        ) from exc

    lower = pinned_overdue + residual_lower
    upper = pinned_overdue + residual_upper
    status = matching.classify(lower, upper, session.limit_count)
    return pinned_overdue, residual_lower, residual_upper, lower, upper, status


def _insert_session(
    conn: sqlite3.Connection,
    parsed: ParsedTable,
    threshold: int,
    limit_count: int,
    source: str,
) -> int:
    """在当前事务内建场并生成首版结论（调用方负责事务）。"""
    joins = list(parsed.joins)
    boards = list(parsed.boards)
    matching.validate_structure(joins, boards)
    matching.check_feasible(joins, boards)

    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO sessions
            (filename, minute_base, duration, joins_json, boards_json,
             total_passengers, threshold, limit_count, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parsed.filename,
            parsed.minute_base,
            parsed.duration,
            json.dumps(joins),
            json.dumps(boards),
            sum(joins),
            threshold,
            limit_count,
            source,
            now,
        ),
    )
    session_id = int(cursor.lastrowid)
    session_row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    session = _session_from_row(session_row)
    pinned, res_lower, res_upper, lower, upper, status = _compute(session, [])
    conn.execute(
        """
        INSERT INTO results
            (session_id, lower, upper, status, pinned_overdue,
             residual_lower, residual_upper, version, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (session_id, lower, upper, status, pinned, res_lower, res_upper, now),
    )
    return session_id


def create_session(
    db_path: str | None,
    parsed: ParsedTable,
    threshold: int = matching.DEFAULT_THRESHOLD,
    limit_count: int = matching.DEFAULT_LIMIT,
    source: str = "csv",
) -> int:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        session_id = _insert_session(conn, parsed, threshold, limit_count, source)
        conn.execute("COMMIT")
        return session_id
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def add_credential(
    db_path: str | None,
    session_id: int,
    credential_no: str,
    join_minute: int,
    board_minute: int,
    count: int,
) -> Outcome:
    credential_no = credential_no.strip()
    if not credential_no:
        raise CredentialInfeasibleError("凭证号不能为空")
    if len(credential_no) > 100:
        raise CredentialInfeasibleError("凭证号过长（最多 100 个字符）")
    if count < 1:
        raise CredentialInfeasibleError("人数必须至少为 1")
    if join_minute < 0 or board_minute < 0:
        raise CredentialInfeasibleError("分钟号不能为负")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = _load_session(conn, session_id)
        if not (session.minute_base <= join_minute <= session.last_minute):
            raise CredentialInfeasibleError(
                f"入队分钟应在 {session.minute_base}..{session.last_minute} 之间"
            )
        if not (session.minute_base <= board_minute <= session.last_minute):
            raise CredentialInfeasibleError(
                f"登车分钟应在 {session.minute_base}..{session.last_minute} 之间"
            )
        if board_minute < join_minute:
            raise CredentialInfeasibleError("登车分钟不能早于入队分钟")

        existing_row = conn.execute(
            "SELECT * FROM credentials WHERE session_id = ? AND credential_no = ?",
            (session_id, credential_no),
        ).fetchone()
        if existing_row is not None:
            existing = _credential_from_row(existing_row)
            if (
                existing.join_minute == join_minute
                and existing.board_minute == board_minute
                and existing.count == count
            ):
                result = _load_result(conn, session_id)
                conn.execute("COMMIT")
                return Outcome("duplicate", session_id, existing, result)
            conn.execute("ROLLBACK")
            raise CredentialConflictError(
                f"凭证 {credential_no} 已按入队 {existing.join_minute} 分钟、"
                f"登车 {existing.board_minute} 分钟、{existing.count} 人采信；"
                "本次内容不同，拒绝覆盖"
            )

        accepted = _load_credentials(conn, session_id)
        now = _now()
        new_credential = CredentialInput(
            credential_no=credential_no,
            join_minute=join_minute,
            board_minute=board_minute,
            count=count,
        )
        pinned, res_lower, res_upper, lower, upper, status = _compute(
            session, [*accepted, new_credential]
        )

        cursor = conn.execute(
            """
            INSERT INTO credentials
                (session_id, credential_no, join_minute, board_minute, count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, credential_no, join_minute, board_minute, count, now),
        )
        version_row = conn.execute(
            "SELECT version FROM results WHERE session_id = ?", (session_id,)
        ).fetchone()
        next_version = int(version_row["version"]) + 1
        conn.execute(
            """
            UPDATE results
               SET lower = ?, upper = ?, status = ?, pinned_overdue = ?,
                   residual_lower = ?, residual_upper = ?, version = ?, updated_at = ?
             WHERE session_id = ?
            """,
            (lower, upper, status, pinned, res_lower, res_upper, next_version, now, session_id),
        )
        credential_row = conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        credential = _credential_from_row(credential_row)
        result = _load_result(conn, session_id)
        conn.execute("COMMIT")
        return Outcome("applied", session_id, credential, result)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def list_sessions(db_path: str | None) -> list[tuple[SessionData, ResultData]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.*, r.session_id AS r_session_id, r.lower AS r_lower, r.upper AS r_upper,
                   r.status AS r_status, r.pinned_overdue AS r_pinned,
                   r.residual_lower AS r_res_lower, r.residual_upper AS r_res_upper,
                   r.version AS r_version, r.updated_at AS r_updated_at
              FROM sessions s JOIN results r ON r.session_id = s.id
             ORDER BY s.id DESC
            """
        ).fetchall()
        items: list[tuple[SessionData, ResultData]] = []
        for row in rows:
            session = _session_from_row(row)
            result = ResultData(
                session_id=row["id"],
                lower=row["r_lower"],
                upper=row["r_upper"],
                status=row["r_status"],
                pinned_overdue=row["r_pinned"],
                residual_lower=row["r_res_lower"],
                residual_upper=row["r_res_upper"],
                version=row["r_version"],
                updated_at=row["r_updated_at"],
            )
            items.append((session, result))
        return items
    finally:
        conn.close()


def get_detail(db_path: str | None, session_id: int) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        # 一个读事务内取齐场次、结论和凭证，避免读到写入方两次提交之间的中间状态。
        conn.execute("BEGIN")
        try:
            session = _load_session(conn, session_id)
            result = _load_result(conn, session_id)
            credentials = _load_credentials(conn, session_id)
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()

    minutes = []
    waiting = 0
    for index, minute in enumerate(session.minute_numbers):
        waiting += session.joins[index] - session.boards[index]
        minutes.append(
            {
                "minute": minute,
                "joins": session.joins[index],
                "boards": session.boards[index],
                "queue": waiting,
            }
        )
    return {
        "session": session,
        "result": result,
        "credentials": credentials,
        "minutes": minutes,
        "overdue_credentials": sum(
            c.count for c in credentials if c.is_overdue(session.threshold)
        ),
        "credential_passengers": sum(c.count for c in credentials),
    }


def describe_status(session: SessionData, result: ResultData) -> str:
    limit = session.limit_count
    threshold = session.threshold
    if result.status == matching.STATUS_MEETS:
        return (
            f"任意完整配对下，等待超过 {threshold} 分钟的最多 {result.upper} 人，"
            f"不超过 {limit} 人，整场达标。"
        )
    if result.status == matching.STATUS_EXCEEDS:
        return (
            f"任意完整配对下，等待超过 {threshold} 分钟的最少 {result.lower} 人，"
            f"已经多于 {limit} 人，整场超标。"
        )
    return (
        f"等待超过 {threshold} 分钟的人数在 {result.lower} 到 {result.upper} 人之间，"
        f"是否多于 {limit} 人取决于具体配对，需进一步核查。"
    )


def list_credentials(db_path: str | None, session_id: int) -> list[CredentialData]:
    conn = connect(db_path)
    try:
        _load_session(conn, session_id)
        return _load_credentials(conn, session_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- 人工核对


def mark_session_verified(db_path: str | None, session_id: int) -> SessionData:
    """把场次标记为已人工核对；重复标记幂等。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = _load_session(conn, session_id)
        if session.verified_at is None:
            conn.execute(
                "UPDATE sessions SET verified_at = ? WHERE id = ?",
                (_now(), session_id),
            )
            session = _load_session(conn, session_id)
        conn.execute("COMMIT")
        return session
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 读头对照


def _current_calibration(conn: sqlite3.Connection) -> CalibrationData | None:
    row = conn.execute(
        "SELECT * FROM calibrations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _calibration_from_row(row) if row is not None else None


def current_calibration(db_path: str | None) -> CalibrationData | None:
    conn = connect(db_path)
    try:
        return _current_calibration(conn)
    finally:
        conn.close()


def create_calibration(
    db_path: str | None, session_id: int, table: readers.ReaderTable
) -> int:
    """上传对照读头表：所有可行分法都须逐分钟还原所选场次的入队和登车人数。

    无解或不合法时抛 CalibrationError，不写入任何内容，原对照继续生效；
    有解时新对照在同一事务内成为现行对照。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = _load_session(conn, session_id)
        if session.verified_at is None:
            raise CalibrationError(
                f"场次 #{session_id} 尚未人工核对，不能用作对照"
            )
        if (
            table.minute_base < session.minute_base
            or table.last_minute > session.last_minute
        ):
            raise CalibrationError(
                f"读头表分钟号 {table.minute_base}–{table.last_minute} 超出"
                f"场次 #{session_id} 的分钟范围 "
                f"{session.minute_base}–{session.last_minute}"
            )
        aligned = readers.align_to_range(table, session.minute_base, session.duration)
        bad = readers.minute_total_mismatch(aligned, session.joins, session.boards)
        if bad is not None:
            raise CalibrationError(
                f"第 {session.minute_base + bad} 分钟读头合计 "
                f"{sum(aligned.counts[bad])} 人，与场次入队+登车 "
                f"{session.joins[bad] + session.boards[bad]} 人不一致，"
                "新对照无解，原对照继续生效"
            )
        assignments = readers.find_assignments(aligned, session.joins, session.boards)
        if not assignments:
            raise CalibrationError(
                "没有任何读头方向分法能逐分钟还原该场次的入队和登车人数，"
                "新对照无解，原对照继续生效"
            )
        cursor = conn.execute(
            """
            INSERT INTO calibrations
                (session_id, filename, minute_base, duration,
                 readers_json, assignments_json, counts_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                table.filename,
                session.minute_base,
                session.duration,
                json.dumps(list(aligned.readers)),
                json.dumps(assignments),
                json.dumps([list(row) for row in aligned.counts]),
                _now(),
            ),
        )
        conn.execute("COMMIT")
        return int(cursor.lastrowid)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 读头表导入（预览 + 确认）


def create_preview(db_path: str | None, table: readers.ReaderTable) -> int:
    """用现行对照把读头表换算成分钟表；唯一且整场能配齐才生成预览。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        calibration = _current_calibration(conn)
        if calibration is None:
            raise PreviewError(
                "还没有生效的对照，请先为已人工核对的场次上传对照读头表"
            )
        unknown = [head for head in table.readers if head not in calibration.readers]
        if unknown:
            shown = "、".join(str(head) for head in unknown)
            raise PreviewError(f"读头 {shown} 未在现行对照中出现过，整表退回")
        unique = readers.unique_minute_table(
            table, calibration.readers, calibration.assignments
        )
        if unique is None:
            raise PreviewError(
                "现行对照的各可行分法换算出的分钟表不一致，"
                "无法唯一确定入队和登车人数，整表退回"
            )
        joins, boards = unique
        try:
            matching.check_feasible(joins, boards)
        except matching.InfeasibleError as exc:
            raise PreviewError(f"换算出的分钟表整场配不齐：{exc}，整表退回") from exc
        cursor = conn.execute(
            """
            INSERT INTO import_previews
                (filename, minute_base, duration, readers_json, counts_json,
                 joins_json, boards_json, calibration_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                table.filename,
                table.minute_base,
                table.duration,
                json.dumps(list(table.readers)),
                json.dumps([list(row) for row in table.counts]),
                json.dumps(list(joins)),
                json.dumps(list(boards)),
                calibration.id,
                _now(),
            ),
        )
        conn.execute("COMMIT")
        return int(cursor.lastrowid)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _load_preview(conn: sqlite3.Connection, preview_id: int) -> PreviewData:
    row = conn.execute(
        "SELECT * FROM import_previews WHERE id = ?", (preview_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"预览 {preview_id} 不存在")
    return _preview_from_row(row)


def get_preview(db_path: str | None, preview_id: int) -> PreviewData:
    conn = connect(db_path)
    try:
        return _load_preview(conn, preview_id)
    finally:
        conn.close()


def confirm_preview(db_path: str | None, preview_id: int) -> ConfirmOutcome:
    """确认预览：按落库当时的现行对照复核，仍能唯一得出预览的分钟表才建场。

    已建场的预览再次确认时直接返回原场次，不重复建场。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        preview = _load_preview(conn, preview_id)
        if preview.session_id is not None:
            conn.execute("COMMIT")
            return ConfirmOutcome("existing", preview_id, preview.session_id)
        calibration = _current_calibration(conn)
        if calibration is None:
            raise PreviewStaleError(
                "当前没有生效的对照，无法复核，未建场，请重新预览"
            )
        unknown = [head for head in preview.readers if head not in calibration.readers]
        if unknown:
            shown = "、".join(str(head) for head in unknown)
            raise PreviewStaleError(
                f"读头 {shown} 不在现行对照中，对照可能已更换，未建场，请重新预览"
            )
        unique = readers.unique_minute_table(
            preview.reader_table(), calibration.readers, calibration.assignments
        )
        if unique is None or unique != (preview.joins, preview.boards):
            raise PreviewStaleError(
                "按落库时的现行对照无法唯一得出预览中的分钟表，未建场，请重新预览"
            )
        parsed = ParsedTable(
            filename=preview.filename,
            minute_base=preview.minute_base,
            duration=preview.duration,
            joins=preview.joins,
            boards=preview.boards,
        )
        session_id = _insert_session(
            conn,
            parsed,
            matching.DEFAULT_THRESHOLD,
            matching.DEFAULT_LIMIT,
            source="reader",
        )
        conn.execute(
            "UPDATE import_previews SET session_id = ?, confirmed_at = ? WHERE id = ?",
            (session_id, _now(), preview_id),
        )
        conn.execute("COMMIT")
        return ConfirmOutcome("created", preview_id, session_id)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
