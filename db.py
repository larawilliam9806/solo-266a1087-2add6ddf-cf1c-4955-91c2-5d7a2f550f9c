"""SQLite persistence layer for the author-identity review app.

Design notes
------------
* One SQLite database file (``DB_PATH``) holds everything; WAL mode is used so
  that readers (e.g. a CSV export) never block writers and vice versa.
* ``records``  : rows imported from the CSV (unique record id, source, name, org).
* ``verdicts`` : *every* direct human judgement ("same person" / "different
  person") between two record ids.  Judgements are append-only, except that a
  user may revoke a specific judgement.  Crucially a "same" judgement is never
  folded into an existing group and thrown away: it stays as a real row so the
  grouping can be rebuilt from the surviving judgements after a revoke.
* Grouping is therefore *derived*, never stored: we rebuild connected
  components from the currently active "same" edges on every read.  That makes
  "revoke one judgement, keep all later ones" trivially correct.
* "Different" judgements are hard constraints.  A new "same" judgement is
  rejected if its two endpoints would end up in one component that also
  contains an active "different" edge.  The check and the insert happen inside
  a single ``BEGIN IMMEDIATE`` transaction, so two concurrent decisions cannot
  both slip past each other.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "identity.db"))

# The whole app is served by one process (gunicorn with 1 worker / several
# threads), so a re-entrant lock serialises writers.  The SQLite transaction
# remains the source of truth and would also protect us against a second
# process.
_write_lock = threading.RLock()


class ConflictError(Exception):
    """The decision would put two records judged "different" into one group.

    ``decide()`` reports conflicts as a return value so the request can roll the
    transaction back cleanly; this exception type is kept for callers that
    prefer raising semantics and for symmetry with the other error types.
    """

    def __init__(self, conflict_pairs: list[tuple[str, str]], message: str = ""):
        self.conflict_pairs = conflict_pairs
        super().__init__(message or "conflicts with existing 'different' judgements")


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


@dataclass(frozen=True)
class Record:
    record_id: str
    source: str
    name: str
    org: str


@dataclass(frozen=True)
class Verdict:
    id: int
    kind: str  # "same" | "different"
    a: str
    b: str
    created_at: str


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                source    TEXT NOT NULL,
                name      TEXT NOT NULL,
                org       TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS verdicts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL CHECK (kind IN ('same', 'different')),
                a          TEXT NOT NULL REFERENCES records(record_id),
                b          TEXT NOT NULL REFERENCES records(record_id),
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_verdicts_active
                ON verdicts(revoked_at, kind);
            -- At most one *active* verdict per unordered record pair.  Rows are
            -- always stored with a < b, but min/max keeps legacy data safe too.
            CREATE UNIQUE INDEX IF NOT EXISTS uq_verdict_pair_active
                ON verdicts(min(a, b), max(a, b))
                WHERE revoked_at IS NULL;

            CREATE TABLE IF NOT EXISTS imports (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT,
                imported   INTEGER NOT NULL,
                updated    INTEGER NOT NULL,
                skipped    INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_records(rows: Iterable[Record], filename: str = "") -> dict:
    """Insert-or-update records in a single transaction.

    Record ids are globally unique: if an id is seen again (from any source)
    its descriptive fields are refreshed; duplicate ids *within one file* are
    collapsed to the last occurrence and reported as skipped.
    """
    deduped: dict[str, Record] = {}
    duplicate_rows = 0
    for row in rows:
        if row.record_id in deduped:
            duplicate_rows += 1
        deduped[row.record_id] = row

    inserted = updated = 0
    with _write_lock, connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rec in deduped.values():
                cur = conn.execute("SELECT 1 FROM records WHERE record_id = ?", (rec.record_id,))
                if cur.fetchone():
                    conn.execute(
                        "UPDATE records SET source=?, name=?, org=? WHERE record_id=?",
                        (rec.source, rec.name, rec.org, rec.record_id),
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO records (record_id, source, name, org) VALUES (?,?,?,?)",
                        (rec.record_id, rec.source, rec.name, rec.org),
                    )
                    inserted += 1
            conn.execute(
                "INSERT INTO imports (filename, imported, updated, skipped) VALUES (?,?,?,?)",
                (filename, inserted, updated, duplicate_rows),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"inserted": inserted, "updated": updated, "skipped_duplicates": duplicate_rows}


def _row_to_record(row: sqlite3.Row) -> Record:
    return Record(row["record_id"], row["source"], row["name"], row["org"])


def _row_to_verdict(row: sqlite3.Row) -> Verdict:
    return Verdict(row["id"], row["kind"], row["a"], row["b"], row["created_at"])


def list_records(search: str = "") -> list[Record]:
    with connection() as conn:
        if search:
            like = f"%{search}%"
            cur = conn.execute(
                "SELECT * FROM records WHERE record_id LIKE ? OR name LIKE ? OR org LIKE ? "
                "ORDER BY name, record_id",
                (like, like, like),
            )
        else:
            cur = conn.execute("SELECT * FROM records ORDER BY name, record_id")
        return [_row_to_record(r) for r in cur.fetchall()]


def get_record(record_id: str) -> Record:
    with connection() as conn:
        row = conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"unknown record id: {record_id}")
    return _row_to_record(row)


def list_verdicts(include_revoked: bool = False) -> list[Verdict]:
    sql = "SELECT * FROM verdicts"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    sql += " ORDER BY id"
    with connection() as conn:
        return [_row_to_verdict(r) for r in conn.execute(sql).fetchall()]


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic representative keeps group ids stable.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _components(record_ids: Iterable[str], same_edges: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Map every record id to a stable group key (smallest member id)."""
    uf = _UnionFind(record_ids)
    for a, b in same_edges:
        uf.union(a, b)
    return {rid: uf.find(rid) for rid in uf.parent}


@dataclass
class GroupView:
    group_key: str
    member_ids: list[str]

    @property
    def label(self) -> str:
        return self.member_ids[0]


def compute_groups() -> tuple[list[GroupView], dict[str, str]]:
    """Return (groups, record_id -> group_key) using *active* same verdicts."""
    with connection() as conn:
        records = [_row_to_record(r) for r in conn.execute("SELECT * FROM records").fetchall()]
        same = conn.execute(
            "SELECT a, b FROM verdicts WHERE revoked_at IS NULL AND kind='same'"
        ).fetchall()
    ids = [r.record_id for r in records]
    mapping = _components(ids, [(r["a"], r["b"]) for r in same])
    buckets: dict[str, list[str]] = defaultdict(list)
    for rid, key in mapping.items():
        buckets[key].append(rid)
    groups = [GroupView(key, sorted(members)) for key, members in sorted(buckets.items())]
    return groups, mapping


def group_summary() -> list[dict]:
    """Groups enriched with the record data, for display."""
    with connection() as conn:
        records = {row["record_id"]: _row_to_record(row) for row in conn.execute("SELECT * FROM records")}
        same = conn.execute(
            "SELECT a, b FROM verdicts WHERE revoked_at IS NULL AND kind='same'"
        ).fetchall()
        diff = conn.execute(
            "SELECT a, b FROM verdicts WHERE revoked_at IS NULL AND kind='different'"
        ).fetchall()
    mapping = _components(records.keys(), [(r["a"], r["b"]) for r in same])
    group_of = mapping
    buckets: dict[str, list[str]] = defaultdict(list)
    for rid, key in mapping.items():
        buckets[key].append(rid)

    verdicts = list_verdicts()
    verdicts_by_record: dict[str, list[Verdict]] = defaultdict(list)
    for v in verdicts:
        verdicts_by_record[v.a].append(v)
        verdicts_by_record[v.b].append(v)

    diff_pairs = [(r["a"], r["b"]) for r in diff]

    out = []
    for key, members in sorted(buckets.items()):
        members = sorted(members)
        out.append(
            {
                "group_key": key,
                "records": [records[m] for m in members],
                "verdicts": sorted(
                    {v.id: v for m in members for v in verdicts_by_record.get(m, [])}.values(),
                    key=lambda v: v.id,
                ),
                "different_to": _invariant_violations(members, diff_pairs),
            }
        )
    return out


def _invariant_violations(members: list[str], diff_pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Active 'different' pairs whose two endpoints ended up in the same group.

    A correct run never produces one; the check exists so a hand-edited database
    or a future bug is surfaced in the UI instead of silently producing bad
    mappings.
    """
    member_set = set(members)
    return [(a, b) for a, b in diff_pairs if a in member_set and b in member_set]


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _active_same_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (r["a"], r["b"])
        for r in conn.execute(
            "SELECT a, b FROM verdicts WHERE revoked_at IS NULL AND kind='same'"
        )
    ]


def _same_conflict_pairs(
    conn: sqlite3.Connection, a: str, b: str
) -> list[tuple[str, str]]:
    """Active different-pairs that would end up inside the merged component.

    Returns the offending record pairs so the UI can name them.  Both records
    of every pair lie in the merged component (component(a) ∪ component(b)).
    The caller must already have revoked any previous verdict for (a, b).
    """
    same = _active_same_pairs(conn)
    ids = {r["record_id"] for r in conn.execute("SELECT record_id FROM records")}
    mapping = _components(ids, same + [(a, b)])
    merged_root = mapping[a]
    diffs = conn.execute(
        "SELECT a, b FROM verdicts WHERE revoked_at IS NULL AND kind='different'"
    ).fetchall()
    conflicts = []
    for row in diffs:
        if mapping.get(row["a"]) == merged_root and mapping.get(row["b"]) == merged_root:
            conflicts.append((row["a"], row["b"]))
    return conflicts


def _different_conflict_pairs(
    conn: sqlite3.Connection, a: str, b: str
) -> list[tuple[str, str]]:
    """A 'different' judgement is impossible if a and b already share a group.

    The caller must already have revoked any previous verdict for (a, b);
    otherwise the pair's own old 'same' edge would make this always fail.
    """
    same = _active_same_pairs(conn)
    ids = {r["record_id"] for r in conn.execute("SELECT record_id FROM records")}
    mapping = _components(ids, same)
    if mapping.get(a) == mapping.get(b):
        return [(a, b)]
    return []


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def decide(a: str, b: str, kind: str) -> dict:
    """Record a 'same' or 'different' judgement.

    Re-deciding a pair drops the previous *active* verdict for that pair while
    keeping it in the table as revoked history.  Revoking the old row happens
    *before* the conflict check, so switching a pair from "same" to
    "different" is judged against the remaining constraints only.

    The whole operation runs under ``BEGIN IMMEDIATE`` plus the process lock.
    """
    if kind not in ("same", "different"):
        raise ValidationError(f"invalid verdict kind: {kind}")
    if a == b:
        raise ValidationError("a record cannot be judged against itself")

    with _write_lock, connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rid in (a, b):
                if not conn.execute(
                    "SELECT 1 FROM records WHERE record_id=?", (rid,)
                ).fetchone():
                    raise NotFoundError(f"unknown record id: {rid}")

            lo, hi = _pair_key(a, b)
            # Drop this pair's previous active verdict first: it must not take
            # part in the constraint check for the new decision.
            conn.execute(
                "UPDATE verdicts SET revoked_at=datetime('now') "
                "WHERE revoked_at IS NULL AND min(a,b)=? AND max(a,b)=?",
                (lo, hi),
            )
            if kind == "same":
                conflicts = _same_conflict_pairs(conn, a, b)
            else:
                conflicts = _different_conflict_pairs(conn, a, b)
            if conflicts:
                conn.execute("ROLLBACK")
                return {
                    "ok": False,
                    "reason": "conflict",
                    "conflict_pairs": conflicts,
                }

            cur = conn.execute(
                "INSERT INTO verdicts (kind, a, b, created_at) VALUES (?,?,?,datetime('now'))",
                (kind, lo, hi),
            )
            conn.execute("COMMIT")
            return {"ok": True, "verdict_id": cur.lastrowid, "kind": kind, "a": lo, "b": hi}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def revoke(verdict_id: int) -> dict:
    """Mark one verdict revoked.  All other verdicts stay exactly as they are."""
    with _write_lock, connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM verdicts WHERE id=?", (verdict_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown verdict id: {verdict_id}")
            if row["revoked_at"] is not None:
                conn.execute("COMMIT")
                return {"ok": True, "already_revoked": True, "verdict_id": verdict_id}
            conn.execute(
                "UPDATE verdicts SET revoked_at=datetime('now') WHERE id=?", (verdict_id,)
            )
            conn.execute("COMMIT")
            return {"ok": True, "already_revoked": False, "verdict_id": verdict_id}
        except Exception:
            conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_rows() -> list[tuple[str, str]]:
    """(record_id, group_key) for every record, read from a single snapshot.

    A read transaction pins a consistent view of both tables, so a concurrent
    decision can never produce a CSV that mixes pre- and post-change groups.
    """
    with connection() as conn:
        conn.execute("BEGIN")
        try:
            records = [r["record_id"] for r in conn.execute("SELECT record_id FROM records")]
            same = [
                (r["a"], r["b"])
                for r in conn.execute(
                    "SELECT a, b FROM verdicts WHERE revoked_at IS NULL AND kind='same'"
                )
            ]
            mapping = _components(records, same)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return [(rid, mapping[rid]) for rid in sorted(mapping, key=lambda r: (mapping[r], r))]
