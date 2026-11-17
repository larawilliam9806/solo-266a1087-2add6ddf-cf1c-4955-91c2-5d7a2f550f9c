from __future__ import annotations

import os
import tempfile

import pytest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="author-ident-test-"), "test.db")

import app as app_module  # noqa: E402
import db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


def _seed():
    db.import_records(
        [
            db.Record("A1", "CNKI", "张伟", "清华计算机"),
            db.Record("A2", "万方", "张伟", "清华 计算机系"),
            db.Record("A3", "Scopus", "Wei Zhang", "Tsinghua CS"),
            db.Record("B1", "Crossref", "李娜", "北大信科"),
            db.Record("B2", "CNKI", "李娜", "北大生命科学"),
            db.Record("C1", "Crossref", "王芳", "复旦数学"),
            db.Record("D1", "CNKI", "陈晨", "浙大医学院"),
        ]
    )


def groups_as_sets():
    return {frozenset(r.record_id for r in g["records"]) for g in db.group_summary()}


def test_import_parses_and_upserts(client):
    csv_body = (
        "唯一记录ID,来源,姓名,单位\n"
        "X1,CNKI,张三,清华大学\n"
        "X2,万方,张三,清华大学\n"
    )
    resp = client.post(
        "/import", data={"file": (__import__("io").BytesIO(csv_body.encode()), "a.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert "导入完成".encode() in resp.data
    assert {r.record_id for r in db.list_records()} == {"X1", "X2"}

    # Re-import with a changed org: updates in place, no duplicate.
    csv_body2 = "record_id,source,name,org\nX1,CNKI,张三,清华大学计算机系\n"
    client.post(
        "/import", data={"file": (__import__("io").BytesIO(csv_body2.encode()), "b.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    got = db.get_record("X1")
    assert got.org == "清华大学计算机系"
    assert len(db.list_records()) == 2


def test_import_rejects_missing_columns(client):
    resp = client.post(
        "/import",
        data={"file": (__import__("io").BytesIO(b"id,name\n1,foo\n"), "bad.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert "导入失败".encode() in resp.data
    assert db.list_records() == []


def test_same_verdict_is_transitive(client):
    _seed()
    assert db.decide("A1", "A2", "same")["ok"]
    assert db.decide("A2", "A3", "same")["ok"]
    assert frozenset({"A1", "A2", "A3"}) in groups_as_sets()


def test_redundant_direct_same_is_saved_after_transitive_grouping(client):
    """A direct judgement between already-indirectly-linked records must persist."""
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A2", "A3", "same")
    before = {v.id for v in db.list_verdicts()}
    res = db.decide("A1", "A3", "same")
    assert res["ok"]
    after = db.list_verdicts()
    assert {v.id for v in after} > before
    direct = [v for v in after if {v.a, v.b} == {"A1", "A3"}]
    assert len(direct) == 1 and direct[0].kind == "same"


def test_revoke_recomputes_grouping_and_keeps_later_judgements(client):
    _seed()
    v12 = db.decide("A1", "A2", "same")["verdict_id"]
    v23 = db.decide("A2", "A3", "same")["verdict_id"]
    v13 = db.decide("A1", "A3", "same")["verdict_id"]
    assert frozenset({"A1", "A2", "A3"}) in groups_as_sets()

    # Revoking A2-A3 leaves A1-A3 direct edge, so the group stays intact.
    db.revoke(v23)
    assert frozenset({"A1", "A2", "A3"}) in groups_as_sets()

    # Revoking the direct A1-A3 edge too splits into {A1, A2} and {A3},
    # i.e. the remaining A1-A2 judgement survived both revocations.
    db.revoke(v13)
    sets = groups_as_sets()
    assert frozenset({"A1", "A2"}) in sets
    assert frozenset({"A3"}) in sets
    active_ids = {v.id for v in db.list_verdicts()}
    assert v12 in active_ids and v23 not in active_ids and v13 not in active_ids


def test_different_conflict_is_rejected_and_points_to_pair(client):
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A3", "B1", "different")
    db.decide("B1", "A2", "same")
    # A3 is now indirectly linked with A2/A1/B1 through B1; making A1-A3 same
    # would put the different pair (A3, B1) in one group.
    res = db.decide("A1", "A3", "same")
    assert res["ok"] is False
    assert res["conflict_pairs"] == [("A3", "B1")] or set(map(frozenset, res["conflict_pairs"])) == {
        frozenset({"A3", "B1"})
    }
    # Original grouping untouched: no A1-A3 same verdict was stored.
    active = db.list_verdicts()
    assert not any({v.a, v.b} == {"A1", "A3"} for v in active)
    assert frozenset({"A1", "A2", "B1"}) in groups_as_sets()


def test_redoing_the_same_pair_changes_its_verdict(client):
    """Re-deciding the *same pair* is an explicit correction (old row revoked)."""
    _seed()
    v_same = db.decide("A1", "A2", "same")["verdict_id"]
    res = db.decide("A1", "A2", "different")
    assert res["ok"] is True
    active = db.list_verdicts()
    assert [(v.kind, v.a, v.b) for v in active] == [("different", "A1", "A2")]
    assert v_same not in {v.id for v in active}
    assert frozenset({"A1"}) in groups_as_sets()


def test_indirect_different_inside_group_is_rejected(client):
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A2", "A3", "same")
    res = db.decide("A1", "A3", "different")
    assert res["ok"] is False
    assert set(map(frozenset, res["conflict_pairs"])) == {frozenset({"A1", "A3"})}
    assert frozenset({"A1", "A2", "A3"}) in groups_as_sets()


def test_atomicity_of_conflicting_decisions(client):
    """A conflicting same-decisions leaves no partial state and no active verdict."""
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("B1", "B2", "same")
    db.decide("A2", "B1", "different")
    res = db.decide("A1", "B2", "same")
    assert res["ok"] is False
    active = db.list_verdicts()
    assert len([v for v in active if v.kind == "same"]) == 2
    assert len([v for v in active if v.kind == "different"]) == 1


def test_export_snapshot_is_consistent_and_matches_groups(client):
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A1", "A3", "same")
    db.decide("B1", "B2", "different")
    resp = client.get("/export.csv")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8-sig")
    rows = [line.split(",") for line in text.strip().splitlines()]
    assert rows[0] == ["record_id", "group_id"]
    mapping = {r[0]: r[1] for r in rows[1:]}
    assert mapping["A1"] == mapping["A2"] == mapping["A3"]
    assert mapping["B1"] != mapping["A1"]
    # Same output as the in-memory grouping.
    groups, expected = db.compute_groups()
    assert mapping == expected


def test_persistence_across_reopen(client, tmp_path):
    path = str(tmp_path / "persist.db")
    db.DB_PATH = path
    db.init_db()
    db.import_records([db.Record("P1", "s", "n", "o"), db.Record("P2", "s", "n", "o")])
    db.decide("P1", "P2", "same")
    db.DB_PATH = path  # simulate restart by re-reading from the same file
    assert frozenset({"P1", "P2"}) in groups_as_sets()


def test_duplicate_active_verdicts_impossible(client):
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A1", "A2", "same")
    db.decide("A2", "A1", "same")
    active = [v for v in db.list_verdicts() if {v.a, v.b} == {"A1", "A2"}]
    assert len(active) == 1
    # History is preserved for auditing.
    assert len([v for v in db.list_verdicts(include_revoked=True) if {v.a, v.b} == {"A1", "A2"}]) == 3


def test_concurrent_conflicting_decisions_never_violate_invariant(client):
    """Many threads race to merge two groups that a 'different' verdict separates."""
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("B1", "B2", "same")
    db.decide("A2", "B1", "different")

    import threading

    results = []
    lock = threading.Lock()

    def worker(x, y):
        out = db.decide(x, y, "same")
        with lock:
            results.append((frozenset({x, y}), out))

    threads = [
        threading.Thread(target=worker, args=("A1", "B2")),
        threading.Thread(target=worker, args=("A2", "B2")),
        threading.Thread(target=worker, args=("A1", "B1")),
        threading.Thread(target=worker, args=("A1", "B2")),
        threading.Thread(target=worker, args=("A2", "B2")),
        threading.Thread(target=worker, args=("A1", "A2")),
        threading.Thread(target=worker, args=("B1", "B2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every cross-group merge is refused; re-asserting an existing judgement
    # inside a group is harmless and may succeed.
    cross_pairs = {frozenset({"A1", "B2"}), frozenset({"A2", "B2"}),
                   frozenset({"A1", "B1"}), frozenset({"A2", "B1"})}
    cross = [out for pair, out in results if pair in cross_pairs]
    assert cross and all(r["ok"] is False for r in cross)
    # Invariant: no active 'different' pair shares a group.
    groups, mapping = db.compute_groups()
    active_diff = [v for v in db.list_verdicts() if v.kind == "different"]
    assert active_diff
    for v in active_diff:
        assert mapping[v.a] != mapping[v.b]
    assert frozenset({"A1", "A2"}) in groups_as_sets()
    assert frozenset({"B1", "B2"}) in groups_as_sets()


def test_export_under_concurrent_changes_never_mixes_states(client):
    _seed()
    import threading
    import time

    valid = [
        {"A1": "A1", "A2": "A2", "A3": "A3"},
        {"A1": "A1", "A2": "A1", "A3": "A3"},
        {"A1": "A1", "A2": "A1", "A3": "A1"},
    ]
    stop = threading.Event()
    seen = []

    def exporter():
        while not stop.is_set():
            mapping = dict(db.export_rows())
            seen.append({k: mapping[k] for k in ("A1", "A2", "A3")})

    t = threading.Thread(target=exporter)
    t.start()
    time.sleep(0.05)
    db.decide("A1", "A2", "same")
    time.sleep(0.05)
    db.decide("A2", "A3", "same")
    time.sleep(0.05)
    stop.set()
    t.join()
    assert seen
    for snap in seen:
        assert snap in valid, snap


def test_http_conflict_message_and_revoke(client):
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A3", "B1", "different")
    db.decide("B1", "A2", "same")
    resp = client.post("/decide", data={"a": "A1", "b": "A3", "kind": "same"},
                       follow_redirects=True)
    body = resp.data.decode()
    assert "冲突" in body and "A3" in body and "B1" in body
    # Grouping stayed the same
    resp = client.get("/")
    assert "A1" in resp.data.decode()

    v = next(v for v in db.list_verdicts() if v.kind == "different")
    resp = client.post(f"/revoke/{v.id}", follow_redirects=True)
    assert "已撤回".encode() in resp.data
    assert db.decide("A1", "A3", "same")["ok"] is True


def test_revoke_unknown_verdict(client):
    resp = client.post("/revoke/99999", follow_redirects=True)
    assert "unknown verdict".encode() in resp.data


def test_decision_view_shows_groups_and_flash(client):
    _seed()
    db.decide("A1", "A2", "same")
    resp = client.get("/")
    body = resp.data.decode()
    assert "当前分组" in body
    assert "A1" in body and "A2" in body
    assert "同一人" in body


def test_back_parameter_cannot_redirect_offsite(client):
    _seed()
    db.decide("A1", "A2", "same")
    resp = client.post(
        "/decide",
        data={"a": "A1", "b": "A2", "kind": "same", "back": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "evil.example" not in resp.headers["Location"]


def test_different_conflict_http_message(client):
    _seed()
    db.decide("A1", "A2", "same")
    db.decide("A2", "A3", "same")
    resp = client.post("/decide", data={"a": "A1", "b": "A3", "kind": "different"},
                       follow_redirects=True)
    body = resp.data.decode()
    assert "冲突" in body and "同一分组" in body
    db.decide("A1", "A2", "same")
    db.decide("A1", "A2", "same")
