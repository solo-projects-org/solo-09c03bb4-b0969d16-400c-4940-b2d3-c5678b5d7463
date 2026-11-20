"""网页与 API 的端到端测试。"""

from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

from app import csv_import, matching, service
from app.db import init_db
from app.main import app


CSV_HEADER = "分钟号,入队人数,登车人数\n"


def build_csv(joins, boards, base: int = 0) -> bytes:
    lines = [CSV_HEADER.strip()]
    for index, (joined, boarded) in enumerate(zip(joins, boards)):
        lines.append(f"{base + index},{joined},{boarded}")
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    init_db(str(db_path))
    with TestClient(app) as test_client:
        yield test_client


def upload(client, joins, boards, base: int = 0, filename: str = "sample.csv"):
    return client.post(
        "/api/sessions",
        files={"file": (filename, build_csv(joins, boards, base), "text/csv")},
    )


# ------------------------------------------------------------ CSV 解析


def test_csv_accepts_header_and_chinese_columns():
    data = "分钟,到达人数,上车人数\n0,2,0\n1,0,2\n".encode("utf-8")
    # 中文列名中“到达人数”是入队，“上车人数”是登车
    parsed = csv_import.parse_csv(data, "x.csv")
    assert parsed.minute_base == 0
    assert parsed.joins == (2, 0)
    assert parsed.boards == (0, 2)


def test_csv_accepts_gbk_encoding():
    text = "minute,joins,boards\n0,1,0\n1,0,1\n"
    parsed = csv_import.parse_csv(text.encode("gb18030"), "gbk.csv")
    assert parsed.joins == (1, 0)
    assert parsed.boards == (0, 1)


def test_csv_rejects_missing_minutes_by_default():
    data = "分钟号,入队人数,登车人数\n0,1,0\n2,0,1\n".encode("utf-8")
    parsed = csv_import.parse_csv(data, "gap.csv")
    # 缺行按该分钟 0 人补，总数仍然配得齐
    assert parsed.joins == (1, 0, 0)
    assert parsed.boards == (0, 0, 1)


def test_csv_rejects_infeasible_table():
    data = "分钟号,入队人数,登车人数\n0,1,0\n1,0,2\n".encode("utf-8")
    with pytest.raises(csv_import.CsvError):
        csv_import.parse_csv(data, "bad.csv")


def test_csv_rejects_negative_and_duplicate():
    with pytest.raises(csv_import.CsvError):
        csv_import.parse_csv(
            "分钟号,入队人数,登车人数\n0,-1,0\n".encode("utf-8"), "neg.csv"
        )
    with pytest.raises(csv_import.CsvError):
        csv_import.parse_csv(
            "分钟号,入队人数,登车人数\n0,1,0\n0,0,1\n".encode("utf-8"), "dup.csv"
        )


def test_csv_rejects_too_long_scene():
    lines = ["分钟号,入队人数,登车人数"]
    for minute in range(0, 122):
        lines.append(f"{minute},1,1")
    with pytest.raises(csv_import.CsvError):
        csv_import.parse_csv(("\n".join(lines) + "\n").encode("utf-8"), "long.csv")


def test_csv_rejects_wrong_column_count():
    with pytest.raises(csv_import.CsvError):
        csv_import.parse_csv("分钟号,入队人数\n0,1\n".encode("utf-8"), "cols.csv")


# ------------------------------------------------------------ 网页


def test_home_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "景区接驳车等待核查" in response.text


def test_upload_creates_session_and_detail_page(client):
    joins = [0, 2, 0, 0, 0, 0]
    boards = [0, 0, 0, 1, 0, 1]
    response = upload(client, joins, boards)
    assert response.status_code == 201, response.text
    payload = response.json()
    session_id = payload["session"]["id"]
    assert payload["result"]["status"] in {"meets", "exceeds", "review"}

    page = client.get(f"/sessions/{session_id}")
    assert page.status_code == 200
    assert "闸机分钟明细" in page.text
    assert "补录抽查凭证" in page.text


def test_upload_rejects_infeasible_with_422(client):
    response = upload(client, [0, 1], [1, 0])
    assert response.status_code == 422
    assert "整表退回" in response.json()["detail"]
    assert client.get("/api/sessions").json()["sessions"] == []


def test_form_upload_redirect(client):
    response = client.post(
        "/sessions",
        files={"file": ("a.csv", build_csv([1, 0], [0, 1]), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/sessions/")


# ------------------------------------------------------------ 结论


def make_review_session(client):
    """构造一个上下界跨越阈值的场次。"""
    # 17 名乘客，超时人数区间 [1, 11]
    arrivals = [5, 19, 30, 30, 6, 3, 24, 7, 29, 26, 30, 14, 11, 16, 11, 28, 16]
    boards = [13, 43, 44, 33, 24, 26, 48, 32, 40, 35, 31, 27, 13, 22, 21, 44, 35]
    base = min(arrivals + boards)
    size = max(arrivals + boards) - base + 1
    joins = [0] * size
    board_counts = [0] * size
    for minute in arrivals:
        joins[minute - base] += 1
    for minute in boards:
        board_counts[minute - base] += 1
    response = upload(client, joins, board_counts, base=base)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["result"]["status"] == "review"
    return payload["session"]["id"], payload


def test_status_meets(client):
    joins = [1, 0, 0, 0, 0]
    boards = [0, 0, 0, 0, 1]
    payload = upload(client, joins, boards).json()
    assert payload["result"]["status"] == "meets"
    assert payload["result"]["lower"] == 0
    assert payload["result"]["upper"] == 0


def test_status_review_bounds(client):
    _, payload = make_review_session(client)
    assert payload["result"]["lower"] == 1
    assert payload["result"]["upper"] == 11


def test_status_exceeds(client):
    arrivals = [22, 19, 22, 5, 15, 1, 23, 21, 8, 14, 5, 6, 24, 21, 21, 7, 8, 20, 8, 13]
    boards = [48, 44, 28, 7, 37, 7, 42, 45, 13, 48, 8, 30, 56, 46, 31, 39, 43, 44, 41, 46]
    base = min(arrivals + boards)
    size = max(arrivals + boards) - base + 1
    joins = [0] * size
    board_counts = [0] * size
    for minute in arrivals:
        joins[minute - base] += 1
    for minute in boards:
        board_counts[minute - base] += 1
    payload = upload(client, joins, board_counts, base=base).json()
    assert payload["result"]["status"] == "exceeds"
    assert payload["result"]["lower"] > 10


def test_queue_column_on_detail_page(client):
    joins = [0, 3, 0, 0]
    boards = [0, 0, 1, 2]
    payload = upload(client, joins, boards).json()
    page = client.get(f"/sessions/{payload['session']['id']}")
    assert "分钟末排队人数" in page.text


# ------------------------------------------------------------ 凭证


def test_credential_changes_bounds(client):
    session_id, before = make_review_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={
            "credential_no": "V-001",
            "join_minute": 5,
            "board_minute": 21,
            "count": 1,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"] == "applied"
    assert body["pinned_overdue"] == 1
    assert body["lower"] == body["pinned_overdue"] + body["residual_lower"]
    assert body["upper"] == body["pinned_overdue"] + body["residual_upper"]
    assert body["version"] == 2


def test_credential_duplicate_only_counted_once(client):
    session_id, _ = make_review_session(client)
    payload = {
        "credential_no": "V-009",
        "join_minute": 3,
        "board_minute": 13,
        "count": 1,
    }
    first = client.post(f"/api/sessions/{session_id}/credentials", json=payload)
    second = client.post(f"/api/sessions/{session_id}/credentials", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["outcome"] == "duplicate"
    assert second.json()["version"] == first.json()["version"]
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert len(detail["credentials"]) == 1


def test_credential_conflict(client):
    session_id, _ = make_review_session(client)
    base = {"credential_no": "V-010", "join_minute": 3, "board_minute": 13, "count": 1}
    client.post(f"/api/sessions/{session_id}/credentials", json=base)
    changed = dict(base, count=2)
    response = client.post(f"/api/sessions/{session_id}/credentials", json=changed)
    assert response.status_code == 409
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["credentials"][0]["count"] == 1


def test_credential_rejected_when_cannot_balance(client):
    # 全场仅 1 人，凭证给 2 人无法核销
    payload = upload(client, [1, 0], [0, 1]).json()
    session_id = payload["session"]["id"]
    response = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "X", "join_minute": 0, "board_minute": 1, "count": 2},
    )
    assert response.status_code == 422
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["credentials"] == []
    assert detail["result"]["version"] == 1


def test_credential_infeasible_leaves_no_trace(client):
    session_id, before = make_review_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "BAD", "join_minute": 40, "board_minute": 10, "count": 1},
    )
    assert response.status_code == 422
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["credentials"] == []
    assert detail["result"]["version"] == before["result"]["version"]
    assert detail["result"]["lower"] == before["result"]["lower"]


def test_credential_out_of_range_minute(client):
    session_id, _ = make_review_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "OUT", "join_minute": 900, "board_minute": 901, "count": 1},
    )
    assert response.status_code == 422


def test_form_credential_submission(client):
    session_id, _ = make_review_session(client)
    response = client.post(
        f"/sessions/{session_id}/credentials",
        data={
            "credential_no": "F-1",
            "join_minute": 5,
            "board_minute": 21,
            "count": 1,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/sessions/{session_id}")
    assert "F-1" in page.text


def test_credentials_stack_and_share_denominator(client):
    session_id, _ = make_review_session(client)
    first = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "A", "join_minute": 5, "board_minute": 21, "count": 1},
    ).json()
    second = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "B", "join_minute": 6, "board_minute": 22, "count": 1},
    ).json()
    assert second["version"] == first["version"] + 1
    # 第二个凭证等待 16 分钟，也超过 15 分钟阈值，锁定超时人数随之增加
    assert second["pinned_overdue"] == first["pinned_overdue"] + 1
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["credential_passengers"] == 2


def test_concurrent_credentials_are_serialized(client, tmp_path):
    session_id, before = make_review_session(client)
    results = []
    errors = []

    def worker(no: str, join_minute: int, board_minute: int):
        try:
            outcome = service.add_credential(
                os.environ["DB_PATH"], session_id, no, join_minute, board_minute, 1
            )
            results.append(outcome.outcome)
        except Exception as exc:  # pragma: no cover - 失败时给出诊断
            errors.append(exc)

    combos = [(5, 21), (6, 22), (7, 24), (11, 27)]
    threads = [
        threading.Thread(target=worker, args=(f"T-{i}", join, board))
        for i, (join, board) in enumerate(combos)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert sorted(results) == ["applied"] * 4
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["credential_passengers"] == 4
    final_joins = [m["joins"] for m in detail["minutes"]]
    final_boards = [m["boards"] for m in detail["minutes"]]
    session = service.get_detail(os.environ["DB_PATH"], session_id)["session"]
    pinned, res_lo, res_hi, lower, upper, status = service._compute(
        session, service.get_detail(os.environ["DB_PATH"], session_id)["credentials"]
    )
    assert detail["result"]["lower"] == lower
    assert detail["result"]["upper"] == upper
    assert final_joins == list(session.joins)
    assert final_boards == list(session.boards)


# ------------------------------------------------------------ 重算期间读


def test_reads_see_last_committed_result_during_recompute(client, tmp_path):
    """写事务（重算）进行中，另一个连接仍能读到上一次已提交的结论。"""
    from app.db import connect

    session_id, before = make_review_session(client)
    writer = connect(os.environ["DB_PATH"])
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE results SET lower = 999, upper = 999 WHERE session_id = ?",
            (session_id,),
        )
        snapshot = service.get_detail(os.environ["DB_PATH"], session_id)
        assert snapshot["result"].lower == before["result"]["lower"]
        assert snapshot["result"].version == before["result"]["version"]
        assert len(snapshot["credentials"]) == 0
        writer.execute("ROLLBACK")
        after = service.get_detail(os.environ["DB_PATH"], session_id)
        assert after["result"].lower == before["result"]["lower"]
    finally:
        writer.close()


# ------------------------------------------------------------ 示例数据


@pytest.mark.parametrize(
    "filename,expected_status",
    [
        ("meets.csv", "meets"),
        ("review.csv", "review"),
        ("exceeds.csv", "exceeds"),
    ],
)
def test_sample_files(client, filename, expected_status):
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "samples" / filename
    response = client.post(
        "/api/sessions",
        files={"file": (filename, path.read_bytes(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["result"]["status"] == expected_status


def test_csv_filled_minutes_reported():
    data = "分钟号,入队人数,登车人数\n0,1,0\n2,0,1\n".encode("utf-8")
    parsed = csv_import.parse_csv(data, "gap.csv")
    assert parsed.filled_minutes == (1,)


def test_credential_cannot_use_another_sessions_id(client):
    session_a, _ = make_review_session(client)
    response = client.post(
        "/api/sessions/9999/credentials",
        json={"credential_no": "N", "join_minute": 5, "board_minute": 21, "count": 1},
    )
    assert response.status_code == 404


def test_credential_count_zero_rejected(client):
    session_id, _ = make_review_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "Z", "join_minute": 5, "board_minute": 21, "count": 0},
    )
    assert response.status_code == 422


def test_concurrent_identical_credential_counted_once(client):
    session_id, before = make_review_session(client)
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            outcome = service.add_credential(
                os.environ["DB_PATH"], session_id, "SAME", 5, 21, 1
            )
            with lock:
                outcomes.append(outcome.outcome)
        except Exception as exc:  # pragma: no cover
            with lock:
                outcomes.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["applied"] + ["duplicate"] * 4
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert len(detail["credentials"]) == 1
    assert detail["result"]["version"] == before["result"]["version"] + 1


def test_concurrent_conflicting_credential_rejected(client):
    session_id, _ = make_review_session(client)
    outcomes = []
    lock = threading.Lock()

    def worker(board_minute):
        try:
            outcome = service.add_credential(
                os.environ["DB_PATH"], session_id, "RACE", 5, board_minute, 1
            )
            with lock:
                outcomes.append(("ok", outcome.outcome, board_minute))
        except service.CredentialConflictError:
            with lock:
                outcomes.append(("conflict", None, board_minute))
        except Exception as exc:  # pragma: no cover
            with lock:
                outcomes.append(("error", repr(exc), board_minute))

    threads = [
        threading.Thread(target=worker, args=(minute,)) for minute in (21, 22, 24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    applied = [item for item in outcomes if item[0] == "ok"]
    conflicts = [item for item in outcomes if item[0] == "conflict"]
    assert len(applied) == 1
    assert len(conflicts) == 2
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert len(detail["credentials"]) == 1
    assert detail["credentials"][0]["board_minute"] == applied[0][2]


def test_credential_at_session_boundaries(client):
    # 全场 2 分钟：第 0 分钟入队 1 人，第 1 分钟登车
    payload = upload(client, [1, 0], [0, 1]).json()
    session_id = payload["session"]["id"]
    ok = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "B1", "join_minute": 0, "board_minute": 1, "count": 1},
    )
    assert ok.status_code == 201
    assert ok.json()["pinned_overdue"] == 0
    # 超出本场分钟范围
    out = client.post(
        f"/api/sessions/{session_id}/credentials",
        json={"credential_no": "B2", "join_minute": 0, "board_minute": 2, "count": 1},
    )
    assert out.status_code == 422
