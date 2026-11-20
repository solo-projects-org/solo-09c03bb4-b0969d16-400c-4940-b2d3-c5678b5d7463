"""业务服务：场次、结论与凭证补录。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from . import matching
from .csv_import import ParsedTable
from .db import connect


class NotFoundError(LookupError):
    """场次不存在。"""


class CredentialConflictError(ValueError):
    """同一凭证号出现了不同内容。"""


class CredentialInfeasibleError(ValueError):
    """补录后整场无法配齐，或凭证与闸机记录矛盾。"""


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
    created_at: str

    @property
    def last_minute(self) -> int:
        return self.minute_base + self.duration - 1

    @property
    def minute_numbers(self) -> list[int]:
        return list(range(self.minute_base, self.last_minute + 1))


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


def create_session(
    db_path: str | None,
    parsed: ParsedTable,
    threshold: int = matching.DEFAULT_THRESHOLD,
    limit_count: int = matching.DEFAULT_LIMIT,
) -> int:
    joins = list(parsed.joins)
    boards = list(parsed.boards)
    matching.validate_structure(joins, boards)
    matching.check_feasible(joins, boards)

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = _now()
        cursor = conn.execute(
            """
            INSERT INTO sessions
                (filename, minute_base, duration, joins_json, boards_json,
                 total_passengers, threshold, limit_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
