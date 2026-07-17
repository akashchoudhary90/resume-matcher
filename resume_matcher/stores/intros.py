"""Warm-intro pathfinder + double-opt-in intro lifecycle (docs/RELATIONSHIPS.md Slices AD/AE).

Pathfinder: bounded BFS over the CONSENTED graph (RelationshipStore.neighbours — the shared
_SHAREABLE gate) from a student to a posting's hiring manager, ranked by product of per-edge
strength × recency. A path needs at least one mutual (the broker = path.nodes[1]); a direct
connection needs no intro.

Intro lifecycle is DOUBLE OPT-IN: the student asks, the BROKER is asked first (never auto-exposed),
the broker accepts and writes a job-related vouch, and only then is the student↔target link
revealed to the employer. A declined request is indistinguishable from "no path" to the student
(silent decline), so the pathfinder surface leaks only a boolean.

Authorization note: this module holds lifecycle logic; the ROUTE layer enforces the broker-only
accept/decline identity check and the application-ownership IDOR check (adversarial criticals).

Phase-5 (docs/PHASE5.md §2.3):
  * A2 — a mutual who never granted `warm_intro` is not a broker. `find_paths(broker_ok=...)`
    prunes those paths BEFORE ranking, so the response is shape-identical to "no path": the
    surface must not become an oracle for who did or didn't opt into brokering.
  * SM-M2 — the BINDING broker-consent check runs inside `create`'s own transaction. The route's
    check is advisory: between it and the INSERT the broker can revoke.
  * C2 — `origin` records whether the chosen path leaned on a bridge (alumni/mentorship) edge, so
    the equity reports can compare bridged vs organic intros. It is per-REQUEST metadata, never a
    per-person attribute.
"""
from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from contextlib import closing

from .db import connect, migrate, platform_db_path
from .relationships import EDGE_STRENGTH, RelationshipStore

# C2: a path is 'bridged' iff it leans on an edge that the institution manufactured (a coordinator
# bridge or a mentorship match) rather than one the student's own activity produced.
BRIDGE_EDGE_KINDS = ("alumni_bridge", "mentorship")

MAX_DEPTH = 3
TOP_K = 5
_RECENCY_HALFLIFE_S = 180 * 86400          # 180-day half-life on edge recency
_INTRO_TTL_S = 21 * 86400                  # a request expires unanswered after 21 days
_BROKER_PENDING_CAP = 25                   # max pending inbound intros per broker
_REQUESTER_LIVE_CAP = 20                   # max live outbound requests per student


class IntroError(Exception):
    """Client-correctable problem -> HTTP 400/409 at the route."""


# ---- pure ranking functions (unit-testable, no DB) ------------------------------------------------
def edge_score(kind: str, last_seen_at: float, now: float) -> float:
    base = EDGE_STRENGTH.get(kind, 0.3)
    age = max(0.0, now - (last_seen_at or now))
    recency = 0.5 ** (age / _RECENCY_HALFLIFE_S)
    return base * (0.4 + 0.6 * recency)     # recency scales, never zeroes, an edge


def rank_path(edge_kinds_and_times: list[tuple[str, float]], now: float) -> float:
    """A path is a product of its edge scores — long or stale paths decay fast (a strong+stale
    two-hop can lose to a moderate+fresh one)."""
    score = 1.0
    for kind, seen in edge_kinds_and_times:
        score *= edge_score(kind, seen, now)
    return round(score, 6)


def path_sort_key(path: dict) -> tuple:
    return (-path["score"], path["hops"])


# ---- pathfinder -----------------------------------------------------------------------------------
def path_origin(path: dict) -> str:
    """C2: 'bridged' iff any edge on the chosen path is an institution-manufactured bridge."""
    return ("bridged" if any(k in BRIDGE_EDGE_KINDS for k, _ in path.get("edges", []))
            else "organic")


def find_paths(rel: RelationshipStore, requester_id: int, target_id: int, school_id: int, *,
               max_depth: int = MAX_DEPTH, top_k: int = TOP_K, now: float | None = None,
               broker_ok: Callable[[int], bool] | None = None) -> list[dict]:
    """Bounded BFS from requester to target over consented edges. Returns ranked paths with
    hops>=2 (at least one broker). Each path: {nodes, edges, hops, score, broker}.

    A2: `broker_ok(uid)` decides whether a candidate mutual may be ASKED — routes pass a
    memoized `warm_intro` consent check. Rejected brokers are dropped at path-completion time,
    before ranking, so a path through a non-consenting mutual is indistinguishable from no path
    at all (a post-ranking filter would leak the difference through result counts/scores)."""
    now = now or time.time()
    # adjacency cache to bound DB hits
    cache: dict[int, list[dict]] = {}

    def nbrs(uid: int) -> list[dict]:
        if uid not in cache:
            cache[uid] = rel.neighbours(uid, school_id)
        return cache[uid]

    paths: list[dict] = []
    # frontier holds (current_node, node_path, edge_path)
    frontier = [(requester_id, [requester_id], [])]
    for _depth in range(max_depth):
        nxt = []
        for node, npath, epath in frontier:
            for e in nbrs(node):
                other = e["other"]
                if other in npath:
                    continue                       # no cycles
                new_edges = epath + [(e["kind"], e["last_seen_at"])]
                new_nodes = npath + [other]
                if other == target_id and len(new_nodes) >= 3:   # hops>=2 => has a broker
                    if broker_ok is not None and not broker_ok(new_nodes[1]):
                        continue                   # A2: not a broker -> the path never existed
                    paths.append({
                        "nodes": new_nodes, "edges": new_edges, "hops": len(new_edges),
                        "score": rank_path(new_edges, now), "broker": new_nodes[1]})
                elif other != target_id:
                    nxt.append((other, new_nodes, new_edges))
        # bound the frontier to the most promising partial paths
        nxt.sort(key=lambda t: rank_path(t[2], now), reverse=True)
        frontier = nxt[:64]
    paths.sort(key=path_sort_key)
    # de-dupe by broker (one best path per mutual is enough for the intro decision)
    best_by_broker: dict[int, dict] = {}
    for p in paths:
        if p["broker"] not in best_by_broker:
            best_by_broker[p["broker"]] = p
    return sorted(best_by_broker.values(), key=path_sort_key)[:top_k]


class IntroStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    def _log(self, conn, intro_id: str, actor: int | None, frm: str | None, to: str) -> None:
        conn.execute("INSERT INTO intro_events(intro_id, actor_user_id, from_status, to_status, at)"
                     " VALUES(?,?,?,?,?)", (intro_id, actor, frm, to, time.time()))

    def create(self, *, school_id: int, posting_id: str, application_id: str,
               requester_user_id: int, target_user_id: int, path: dict,
               note_redacted: str | None, origin: str = "organic") -> dict:
        """Create a requested intro to the path's broker. Route has already verified application
        ownership (IDOR) and broker availability.

        SM-M2: the broker's `warm_intro` consent is re-checked HERE, on this connection, inside a
        BEGIN IMMEDIATE — the route's earlier check is advisory because a broker can revoke in the
        window between pathfinding and INSERT. This check is the invariant: no row is ever written
        naming a broker who is not, at commit time, opted in."""
        broker = path["broker"]
        if origin not in ("organic", "bridged"):
            raise IntroError("Unknown intro origin.")
        now = time.time()
        intro_id = secrets.token_urlsafe(10)
        with closing(self._conn()) as conn:
            conn.isolation_level = None            # explicit txn control (BEGIN IMMEDIATE below)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM consents WHERE user_id=? AND purpose='warm_intro' "
                    "AND revoked_at IS NULL", (broker,)).fetchone() is None:
                    # same neutral message as every other broker-unavailable branch (no oracle)
                    raise IntroError("An intro through this connection isn't available.")
                # broker spam caps + block
                if conn.execute("SELECT 1 FROM broker_blocks WHERE broker_user_id=? "
                                "AND blocked_user_id=?",
                                (broker, requester_user_id)).fetchone():
                    raise IntroError("An intro through this connection isn't available.")
                pend = conn.execute("SELECT COUNT(*) FROM intro_requests WHERE broker_user_id=? "
                                    "AND status='requested'", (broker,)).fetchone()[0]
                if pend >= _BROKER_PENDING_CAP:
                    raise IntroError("An intro through this connection isn't available right now.")
                live = conn.execute("SELECT COUNT(*) FROM intro_requests WHERE requester_user_id=? "
                                    "AND status='requested'", (requester_user_id,)).fetchone()[0]
                if live >= _REQUESTER_LIVE_CAP:
                    raise IntroError("You have too many pending intro requests.")
                try:
                    conn.execute(
                        "INSERT INTO intro_requests(id, school_id, posting_id, application_id, "
                        "requester_user_id, target_user_id, broker_user_id, hops, path_score, "
                        "path_json, note_redacted, origin, created_at, expires_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (intro_id, school_id, posting_id, application_id, requester_user_id,
                         target_user_id, broker, path["hops"], path["score"], json.dumps(path),
                         note_redacted, origin, now, now + _INTRO_TTL_S))
                except IntroError:
                    raise
                except Exception as exc:  # UNIQUE(requester, posting)
                    raise IntroError("You already requested an intro for this posting.") from exc
                self._log(conn, intro_id, requester_user_id, None, "requested")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {"intro_id": intro_id, "status": "requested"}

    def get(self, intro_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM intro_requests WHERE id=?", (intro_id,)).fetchone()
        return dict(row) if row else None

    def inbox(self, broker_user_id: int) -> list[dict]:
        """The broker's pending requests — the opt-in reveal of who's asking + for what."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT ir.id, ir.posting_id, p.title, ir.requester_user_id, u.email AS requester_email, "
                "ir.note_redacted, ir.hops, ir.created_at FROM intro_requests ir "
                "JOIN users u ON u.id=ir.requester_user_id "
                "LEFT JOIN postings p ON p.id=ir.posting_id "
                "WHERE ir.broker_user_id=? AND ir.status='requested' ORDER BY ir.created_at",
                (broker_user_id,)).fetchall()
        return [dict(r) for r in rows]

    def mine(self, requester_user_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT ir.id, ir.posting_id, p.title, ir.status, ir.created_at, ir.responded_at, "
                "CASE WHEN ir.status='accepted' THEN ir.broker_user_id ELSE NULL END AS broker_user_id "
                "FROM intro_requests ir LEFT JOIN postings p ON p.id=ir.posting_id "
                "WHERE ir.requester_user_id=? ORDER BY ir.created_at DESC",
                (requester_user_id,)).fetchall()
        return [dict(r) for r in rows]

    def accept(self, intro_id: str, broker_user_id: int, vouch_id: str) -> dict:
        """Broker accepts (route already enforced broker-only). Attaches the vouch and reveals."""
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT status, broker_user_id FROM intro_requests WHERE id=?",
                               (intro_id,)).fetchone()
            if row is None or row["broker_user_id"] != broker_user_id:
                raise IntroError("No such intro request.")
            if row["status"] != "requested":
                raise IntroError(f"That request is already {row['status']}.")
            conn.execute("UPDATE intro_requests SET status='accepted', responded_at=?, vouch_id=?, "
                         "purge_after=? WHERE id=?", (now, vouch_id, now + 180 * 86400, intro_id))
            self._log(conn, intro_id, broker_user_id, "requested", "accepted")
            conn.commit()
        return {"ok": True, "status": "accepted"}

    def decline(self, intro_id: str, broker_user_id: int) -> dict:
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT status, broker_user_id FROM intro_requests WHERE id=?",
                               (intro_id,)).fetchone()
            if row is None or row["broker_user_id"] != broker_user_id:
                raise IntroError("No such intro request.")
            if row["status"] != "requested":
                raise IntroError(f"That request is already {row['status']}.")
            conn.execute("UPDATE intro_requests SET status='declined', responded_at=?, "
                         "purge_after=? WHERE id=?", (now, now + 180 * 86400, intro_id))
            self._log(conn, intro_id, broker_user_id, "requested", "declined")
            conn.commit()
        return {"ok": True}   # route returns the SAME neutral shape as "no path" (silent decline)

    def block(self, broker_user_id: int, blocked_user_id: int) -> None:
        with closing(self._conn()) as conn:
            conn.execute("INSERT OR IGNORE INTO broker_blocks(broker_user_id, blocked_user_id, "
                         "created_at) VALUES(?,?,?)", (broker_user_id, blocked_user_id, time.time()))
            conn.commit()

    def accepted_for_application(self, application_id: str) -> list[dict]:
        """Accepted intros to surface on the employer evidence card (with the linked vouch)."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT ir.broker_user_id, u.role AS broker_role, ir.hops, ir.path_json, ir.vouch_id "
                "FROM intro_requests ir JOIN users u ON u.id=ir.broker_user_id "
                "WHERE ir.application_id=? AND ir.status='accepted'", (application_id,)).fetchall()
        return [dict(r) for r in rows]

    def outcome_rows(self, school_id: int) -> list[dict]:
        """C2 report source: one row per intro request — its origin, the broker edge kind it was
        founded on, its status, and (via the application) where the student ended up. Raw rows
        only; MIN_CELL suppression and consent filtering happen above this (§3.2), because this is
        also the shape the CSV export needs."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT ir.id, ir.origin, ir.status, ir.path_json, ir.requester_user_id, "
                "a.status AS application_status FROM intro_requests ir "
                "LEFT JOIN applications a ON a.id=ir.application_id "
                "WHERE ir.school_id=?", (school_id,)).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            edges = (json.loads(row.pop("path_json") or "{}") or {}).get("edges") or []
            # edges[0] is the requester->broker hop: the relationship the intro actually leaned on
            row["broker_edge_kind"] = edges[0][0] if edges else None
            out.append(row)
        return out

    def sweep_expired(self) -> int:
        now = time.time()
        with closing(self._conn()) as conn:
            cur = conn.execute("UPDATE intro_requests SET status='expired', purge_after=? "
                               "WHERE status='requested' AND expires_at < ?",
                               (now + 180 * 86400, now))
            conn.commit()
            return cur.rowcount
