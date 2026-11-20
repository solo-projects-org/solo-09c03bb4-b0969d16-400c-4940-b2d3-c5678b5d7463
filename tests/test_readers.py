"""读头表解析、方向对照、导入预览与确认建场的测试。"""

from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

from app import csv_import, readers, service
from app.db import init_db
from app.main import app


def build_csv(joins, boards, base: int = 0) -> bytes:
    lines = ["分钟号,入队人数,登车人数"]
    for index, (joined, boarded) in enumerate(zip(joins, boards)):
        lines.append(f"{base + index},{joined},{boarded}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_reader_csv(rows, header: str | None = "分钟号,读头号,人数") -> bytes:
    lines = [header] if header else []
    lines += [f"{minute},{head},{count}" for minute, head, count in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


# 场景 A：读头 1 计入队、读头 2 计登车，分法唯一（"JB"）
JOINS_A = [2, 1, 0, 0]
BOARDS_A = [0, 1, 1, 1]
REF_ROWS_A = [(0, 1, 2), (1, 1, 1), (1, 2, 1), (2, 2, 1), (3, 2, 1)]

# 场景 B：同一套读头方向相反，分法唯一（"BJ"）
JOINS_B = [0, 2, 1, 0]
BOARDS_B = [0, 1, 0, 2]
REF_ROWS_B = [(1, 1, 1), (1, 2, 2), (2, 2, 1), (3, 1, 2)]

# 新一场读头表：按 A 换算 joins=[3,2,0,0]、boards=[0,0,4,1]
NEXT_ROWS = [(0, 1, 3), (1, 1, 2), (2, 2, 4), (3, 2, 1)]
NEXT_JOINS = [3, 2, 0, 0]
NEXT_BOARDS = [0, 0, 4, 1]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    init_db(str(db_path))
    with TestClient(app) as test_client:
        yield test_client


def upload_session(client, joins, boards, base: int = 0, filename: str = "base.csv"):
    response = client.post(
        "/api/sessions",
        files={"file": (filename, build_csv(joins, boards, base), "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()["session"]["id"]


def make_verified_session(client, joins, boards, base: int = 0, filename: str = "base.csv"):
    session_id = upload_session(client, joins, boards, base, filename)
    response = client.post(f"/api/sessions/{session_id}/verify")
    assert response.status_code == 200, response.text
    assert response.json()["verified_at"]
    return session_id


def upload_calibration(client, session_id, rows, filename: str = "ref.csv"):
    return client.post(
        "/api/calibrations",
        data={"session_id": str(session_id)},
        files={"file": (filename, build_reader_csv(rows), "text/csv")},
    )


def make_calibration_a(client):
    session_id = make_verified_session(client, JOINS_A, BOARDS_A)
    response = upload_calibration(client, session_id, REF_ROWS_A)
    assert response.status_code == 201, response.text
    return session_id, response.json()


def upload_preview(client, rows, filename: str = "next.csv"):
    return client.post(
        "/api/imports/preview",
        files={"file": (filename, build_reader_csv(rows), "text/csv")},
    )


# ------------------------------------------------------------ 读头表解析


def test_reader_csv_header_and_zero_fill():
    data = build_reader_csv([(0, 1, 2), (2, 2, 1)])
    parsed = readers.parse_reader_csv(data, "x.csv")
    assert parsed.minute_base == 0
    assert parsed.duration == 3
    assert parsed.readers == (1, 2)
    assert parsed.counts == ((2, 0), (0, 0), (0, 1))
    assert parsed.filled_minutes == (1,)


def test_reader_csv_without_header_and_english_header():
    plain = readers.parse_reader_csv(b"0,1,2\n1,1,1\n", "plain.csv")
    assert plain.readers == (1,)
    assert plain.counts == ((2,), (1,))
    english = readers.parse_reader_csv(
        b"minute,reader,count\n0,1,2\n1,1,1\n", "en.csv"
    )
    assert english.counts == plain.counts


def test_reader_csv_gb18030():
    text = "分钟号,读头号,人数\n0,1,2\n1,1,1\n"
    parsed = readers.parse_reader_csv(text.encode("gb18030"), "gbk.csv")
    assert parsed.counts == ((2,), (1,))


def test_reader_csv_rejects_too_many_readers():
    rows = [(0, head, 1) for head in range(1, 14)]
    with pytest.raises(csv_import.CsvError, match="至多 12"):
        readers.parse_reader_csv(build_reader_csv(rows), "many.csv")


def test_reader_csv_rejects_duplicate_cell():
    with pytest.raises(csv_import.CsvError, match="重复"):
        readers.parse_reader_csv(build_reader_csv([(0, 1, 1), (0, 1, 2)]), "dup.csv")


def test_reader_csv_rejects_negative_and_noninteger():
    with pytest.raises(csv_import.CsvError):
        readers.parse_reader_csv(build_reader_csv([(0, 1, -1)]), "neg.csv")
    with pytest.raises(csv_import.CsvError):
        readers.parse_reader_csv("分钟号,读头号,人数\n0,甲,1\n".encode(), "nan.csv")


def test_reader_csv_rejects_span_over_120():
    with pytest.raises(csv_import.CsvError, match="至多 120 分钟"):
        readers.parse_reader_csv(build_reader_csv([(0, 1, 1), (121, 1, 1)]), "span.csv")


def test_reader_csv_rejects_bad_columns_and_empty():
    with pytest.raises(csv_import.CsvError):
        readers.parse_reader_csv("分钟号,读头号\n0,1\n".encode(), "cols.csv")
    with pytest.raises(csv_import.CsvError):
        readers.parse_reader_csv("".encode(), "empty.csv")


# ------------------------------------------------------------ 分法枚举（纯函数）


def _table(rows, base=0, duration=None):
    parsed = readers.parse_reader_csv(build_reader_csv(rows), "t.csv")
    if duration is None:
        return parsed
    return readers.align_to_range(parsed, base, duration)


def test_find_assignments_unique():
    table = _table(REF_ROWS_A)
    assert readers.find_assignments(table, JOINS_A, BOARDS_A) == ["JB"]


def test_find_assignments_multiple_with_symmetric_readers():
    table = _table([(0, 1, 1), (0, 2, 1), (1, 1, 1), (1, 2, 1)])
    assert readers.find_assignments(table, [1, 1], [1, 1]) == ["JB", "BJ"]


def test_find_assignments_none_when_totals_mismatch():
    table = _table(REF_ROWS_A)
    assert readers.find_assignments(table, [9, 1, 0, 0], BOARDS_A) == []
    assert readers.minute_total_mismatch(table, [9, 1, 0, 0], BOARDS_A) == 0
    assert readers.minute_total_mismatch(table, JOINS_A, BOARDS_A) is None


def test_unique_minute_table_agreement_and_conflict():
    heads = (1, 2)
    agree = _table([(0, 1, 2), (1, 2, 2)])
    # "JB" 与 "BJ" 换算结果不同 -> 不唯一
    assert readers.unique_minute_table(agree, heads, ["BJ", "JB"]) is None
    same = _table([(0, 1, 1), (0, 2, 1)])
    # 两个读头人数完全相同 -> 两种分法换算结果相同
    assert readers.unique_minute_table(same, heads, ["BJ", "JB"]) == ((1,), (1,))


# ------------------------------------------------------------ 对照


def test_calibration_requires_verified_session(client):
    session_id = upload_session(client, JOINS_A, BOARDS_A)
    response = upload_calibration(client, session_id, REF_ROWS_A)
    assert response.status_code == 422
    assert "人工核对" in response.json()["detail"]
    assert client.get("/api/calibrations/current").json()["calibration"] is None


def test_calibration_establishes_current(client):
    session_id, payload = make_calibration_a(client)
    assert payload["readers"] == [1, 2]
    assert payload["assignments"] == ["JB"]
    assert payload["session_id"] == session_id
    current = client.get("/api/calibrations/current").json()["calibration"]
    assert current["id"] == payload["id"]


def test_calibration_unsolvable_keeps_previous(client):
    _, good = make_calibration_a(client)
    other = make_verified_session(client, [1, 0], [0, 1], filename="other.csv")
    # 该场入队 1 人登车 1 人，但读头表每分钟合计对不上 -> 无解
    response = upload_calibration(client, other, [(0, 1, 2)], "bad.csv")
    assert response.status_code == 422
    assert "原对照继续生效" in response.json()["detail"]
    current = client.get("/api/calibrations/current").json()["calibration"]
    assert current["id"] == good["id"]


def test_calibration_minute_mismatch_rejected(client):
    session_id = make_verified_session(client, JOINS_A, BOARDS_A)
    rows = [(0, 1, 2), (1, 1, 1), (1, 2, 1), (2, 2, 1), (3, 2, 2)]  # 第 3 分钟多 1 人
    response = upload_calibration(client, session_id, rows)
    assert response.status_code == 422
    assert "不一致" in response.json()["detail"]
    assert client.get("/api/calibrations/current").json()["calibration"] is None


def test_calibration_out_of_range_minutes_rejected(client):
    session_id = make_verified_session(client, JOINS_A, BOARDS_A)
    response = upload_calibration(client, session_id, [(9, 1, 1)])
    assert response.status_code == 422
    assert "超出" in response.json()["detail"]


def test_calibration_missing_session_404(client):
    response = client.post(
        "/api/calibrations",
        data={"session_id": "999"},
        files={"file": ("ref.csv", build_reader_csv(REF_ROWS_A), "text/csv")},
    )
    assert response.status_code == 404


# ------------------------------------------------------------ 预览


def test_preview_requires_calibration(client):
    response = upload_preview(client, NEXT_ROWS)
    assert response.status_code == 422
    assert "对照" in response.json()["detail"]


def test_preview_unknown_reader_rejected(client):
    make_calibration_a(client)
    response = upload_preview(client, [(0, 9, 1)])
    assert response.status_code == 422
    assert "未在现行对照中出现过" in response.json()["detail"]


def test_preview_non_unique_rejected(client):
    # 对照：两个读头每分钟人数相同，"BJ"/"JB" 都可行
    session_id = make_verified_session(client, [1, 1], [1, 1])
    response = upload_calibration(
        client, session_id, [(0, 1, 1), (0, 2, 1), (1, 1, 1), (1, 2, 1)]
    )
    assert response.status_code == 201, response.text
    assert sorted(response.json()["assignments"]) == ["BJ", "JB"]
    # 新表两个读头人数不同 -> 两种分法换算结果不同
    response = upload_preview(client, [(0, 1, 2), (1, 2, 2)])
    assert response.status_code == 422
    assert "不一致" in response.json()["detail"]


def test_preview_infeasible_rejected(client):
    make_calibration_a(client)
    # 唯一换算出 joins=[0,1]、boards=[1,0]，第 0 分钟登车超过累计入队
    response = upload_preview(client, [(0, 2, 1), (1, 1, 1)])
    assert response.status_code == 422
    assert "配不齐" in response.json()["detail"]


def test_preview_happy_path(client):
    make_calibration_a(client)
    response = upload_preview(client, NEXT_ROWS)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["joins"] == NEXT_JOINS
    assert payload["boards"] == NEXT_BOARDS
    assert payload["total_passengers"] == 5
    assert payload["session_id"] is None


# ------------------------------------------------------------ 确认建场


def make_preview(client, rows=NEXT_ROWS):
    response = upload_preview(client, rows)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_confirm_creates_session(client):
    make_calibration_a(client)
    preview_id = make_preview(client)
    response = client.post(f"/api/imports/{preview_id}/confirm")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["outcome"] == "created"
    assert payload["session"]["source"] == "reader"
    assert payload["session"]["total_passengers"] == 5
    detail = client.get(f"/api/sessions/{payload['session']['id']}").json()
    assert [m["joins"] for m in detail["minutes"]] == NEXT_JOINS
    assert [m["boards"] for m in detail["minutes"]] == NEXT_BOARDS
    preview = client.get(f"/api/imports/{preview_id}").json()
    assert preview["session_id"] == payload["session"]["id"]
    assert preview["confirmed_at"]


def test_confirm_again_returns_original_session(client):
    make_calibration_a(client)
    preview_id = make_preview(client)
    first = client.post(f"/api/imports/{preview_id}/confirm")
    second = client.post(f"/api/imports/{preview_id}/confirm")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["outcome"] == "existing"
    assert second.json()["session"]["id"] == first.json()["session"]["id"]
    assert len(client.get("/api/sessions").json()["sessions"]) == 2  # 对照场 + 新场


def test_confirm_stale_after_calibration_change(client):
    make_calibration_a(client)
    preview_id = make_preview(client)
    # 换一套方向相反的对照（读头 1、2 的方向对调）
    session_b = make_verified_session(client, JOINS_B, BOARDS_B, filename="b.csv")
    response = upload_calibration(client, session_b, REF_ROWS_B, "ref_b.csv")
    assert response.status_code == 201, response.text
    assert response.json()["assignments"] == ["BJ"]
    response = client.post(f"/api/imports/{preview_id}/confirm")
    assert response.status_code == 409
    assert "重新预览" in response.json()["detail"]
    # 未建场，预览仍待确认
    assert client.get(f"/api/imports/{preview_id}").json()["session_id"] is None
    assert len(client.get("/api/sessions").json()["sessions"]) == 2


def test_confirm_succeeds_after_equivalent_recalibration(client):
    make_calibration_a(client)
    preview_id = make_preview(client)
    # 用同一场次同一读头表重建对照（id 变新，分法不变）
    session_a = client.get("/api/calibrations/current").json()["calibration"]["session_id"]
    response = upload_calibration(client, session_a, REF_ROWS_A)
    assert response.status_code == 201, response.text
    response = client.post(f"/api/imports/{preview_id}/confirm")
    assert response.status_code == 201, response.text
    assert response.json()["outcome"] == "created"


def test_concurrent_confirm_creates_one_session(client, tmp_path):
    make_calibration_a(client)
    preview_id = make_preview(client)
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            outcome = service.confirm_preview(os.environ["DB_PATH"], preview_id)
            with lock:
                outcomes.append(outcome.outcome)
        except Exception as exc:  # pragma: no cover
            with lock:
                outcomes.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created"] + ["existing"] * 3
    sessions = client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 2  # 对照场 + 唯一建场


# ------------------------------------------------------------ 网页


def test_index_shows_calibration_and_import_cards(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "读头方向对照" in page.text
    assert "导入读头 CSV" in page.text
    assert "尚未建立对照" in page.text


def test_verify_button_marks_session(client):
    session_id = upload_session(client, JOINS_A, BOARDS_A)
    page = client.get(f"/sessions/{session_id}")
    assert "标记为已人工核对" in page.text
    response = client.post(f"/sessions/{session_id}/verify", follow_redirects=False)
    assert response.status_code == 303
    page = client.get(f"/sessions/{session_id}")
    assert "已人工核对" in page.text
    # 重复标记幂等
    again = client.post(f"/api/sessions/{session_id}/verify")
    assert again.status_code == 200


def test_calibration_form_flow(client):
    session_id = make_verified_session(client, JOINS_A, BOARDS_A)
    response = client.post(
        "/calibrations",
        data={"session_id": str(session_id)},
        files={"file": ("ref.csv", build_reader_csv(REF_ROWS_A), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/")
    assert "现行对照 #1" in page.text
    assert "可行分法 1 种" in page.text


def test_preview_page_and_confirm_form(client):
    make_calibration_a(client)
    response = client.post(
        "/imports/preview",
        files={"file": ("next.csv", build_reader_csv(NEXT_ROWS), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/imports/")
    page = client.get(location.split("?")[0])
    assert page.status_code == 200
    assert "换算后的分钟表" in page.text
    assert "确认建场" in page.text
    confirm = client.post(location.split("?")[0] + "/confirm", follow_redirects=False)
    assert confirm.status_code == 303
    assert confirm.headers["location"].startswith("/sessions/")
    # 再次确认返回原场次
    again = client.post(location.split("?")[0] + "/confirm", follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"].split("?")[0] == confirm.headers["location"].split("?")[0]


def test_confirm_form_stale_redirects_with_error(client):
    make_calibration_a(client)
    preview_id = make_preview(client)
    session_b = make_verified_session(client, JOINS_B, BOARDS_B, filename="b.csv")
    upload_calibration(client, session_b, REF_ROWS_B, "ref_b.csv")
    response = client.post(f"/imports/{preview_id}/confirm", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?")


def test_reader_session_can_be_verified_and_used_as_calibration(client):
    """读头表建出的场次同样可以人工核对并充当新对照。"""
    make_calibration_a(client)
    preview_id = make_preview(client)
    session_id = client.post(f"/api/imports/{preview_id}/confirm").json()["session"]["id"]
    client.post(f"/api/sessions/{session_id}/verify")
    # 该场 joins=[3,2,0,0]、boards=[0,0,4,1]，用同方向读头表可还原
    rows = [(0, 1, 3), (1, 1, 2), (2, 2, 4), (3, 2, 1)]
    response = upload_calibration(client, session_id, rows)
    assert response.status_code == 201, response.text
    assert response.json()["assignments"] == ["JB"]


def test_sample_reader_files(client):
    """samples 里的对照表与 meets.csv 逐分钟一致，下一场可唯一换算。"""
    from pathlib import Path

    samples = Path(__file__).resolve().parent.parent / "samples"
    # 对照场：meets.csv
    response = client.post(
        "/api/sessions",
        files={"file": ("meets.csv", (samples / "meets.csv").read_bytes(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["session"]["id"]
    client.post(f"/api/sessions/{session_id}/verify")
    response = client.post(
        "/api/calibrations",
        data={"session_id": str(session_id)},
        files={"file": ("reader_ref.csv", (samples / "reader_ref.csv").read_bytes(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["assignments"] == ["JB"]
    response = client.post(
        "/api/imports/preview",
        files={"file": ("reader_next.csv", (samples / "reader_next.csv").read_bytes(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    preview_id = response.json()["id"]
    confirm = client.post(f"/api/imports/{preview_id}/confirm")
    assert confirm.status_code == 201, confirm.text
