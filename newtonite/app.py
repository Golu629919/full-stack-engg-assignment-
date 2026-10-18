"""Newtonite Ops - work item coordination. Stdlib only (Python 3.9+)."""
import json, sqlite3, time, threading, secrets, re, os, sys, random
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB = os.environ.get("DB_PATH", "newtonite.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, token TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS teams(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS memberships(user_id INT, team_id INT, role TEXT, PRIMARY KEY(user_id,team_id));
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, team_id INT NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'normal', assignee_id INT, needs_approval INT DEFAULT 0,
  version INT NOT NULL DEFAULT 1, created_by INT, created_at INT, updated_at INT);
CREATE INDEX IF NOT EXISTS ix_items_list ON items(team_id,status,updated_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS ix_items_assignee ON items(assignee_id,status);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, item_id INT, actor_id INT, type TEXT, payload TEXT, created_at INT);
CREATE INDEX IF NOT EXISTS ix_events_item ON events(item_id,id DESC);
CREATE TABLE IF NOT EXISTS idem(user_id INT, key TEXT, status INT, body TEXT, PRIMARY KEY(user_id,key));
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, event_id INT, state TEXT DEFAULT 'pending', attempts INT DEFAULT 0, run_after INT DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, event_id INT, user_id INT, item_id INT, text TEXT, created_at INT, read INT DEFAULT 0, UNIQUE(event_id,user_id));
"""
RANK = {"viewer": 1, "member": 2, "manager": 3, "admin": 4}
PERMS = {"read": 1, "comment": 2, "create": 2, "claim": 2, "assign": 3, "prioritize": 3}
TRANSITIONS = {"open": ["in_progress", "cancelled"], "in_progress": ["review", "open", "cancelled"],
               "review": ["done", "in_progress"], "done": [], "cancelled": []}
PRIORITIES = ["low", "normal", "high", "urgent"]
ACTIVE = "('open','in_progress','review')"
TEST_HOOKS = {"fail_notify": 0}

class ApiError(Exception):
    def __init__(s, status, msg, **extra): s.status, s.msg, s.extra = status, msg, extra

def now(): return int(time.time() * 1000)
def connect():
    c = sqlite3.connect(DB, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=15000")
    return c
def init_db():
    c = connect(); c.executescript(SCHEMA); c.close()

# ---------- authorization ----------
def get_item(c, u, item_id, perm):
    it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    r = c.execute("SELECT role FROM memberships WHERE user_id=? AND team_id=?", (u["id"], it["team_id"])).fetchone() if it else None
    if not r: raise ApiError(404, "not found")          # don't leak existence to non-members
    if RANK[r[0]] < PERMS[perm]: raise ApiError(403, f"requires {perm} permission")
    return it, r[0]
def team_role(c, u, team_id, perm):
    r = c.execute("SELECT role FROM memberships WHERE user_id=? AND team_id=?", (u["id"], team_id)).fetchone()
    if not r: raise ApiError(403, "not a member of that team")
    if RANK[r[0]] < PERMS[perm]: raise ApiError(403, f"requires {perm} permission")
    return r[0]

def log(c, item_id, actor, typ, payload):
    """Append-only history + outbox row, in the SAME transaction as the state change."""
    cur = c.execute("INSERT INTO events(item_id,actor_id,type,payload,created_at) VALUES(?,?,?,?,?)",
                    (item_id, actor, typ, json.dumps(payload), now()))
    c.execute("INSERT INTO outbox(event_id) VALUES(?)", (cur.lastrowid,))

def public(it): return {k: it[k] for k in it.keys()}
def need_str(b, k, mx=200):
    v = b.get(k)
    if not isinstance(v, str) or not v.strip() or len(v) > mx: raise ApiError(422, f"invalid {k}")
    return v.strip()

# ---------- handlers ----------
def list_items(c, u, q):
    where, a = ["i.team_id IN (SELECT team_id FROM memberships WHERE user_id=?)"], [u["id"]]
    for k in ("team_id", "status", "priority"):
        if q.get(k): where.append(f"i.{k}=?"); a.append(q[k])
    if q.get("assignee") == "me": where.append("i.assignee_id=?"); a.append(u["id"])
    if q.get("assignee") == "none": where.append("i.assignee_id IS NULL")
    if q.get("q"): where.append("(i.title LIKE ? OR i.description LIKE ?)"); a += [f"%{q['q']}%"] * 2
    if q.get("cursor"):
        ts, i = q["cursor"].split(":"); where.append("(i.updated_at,i.id)<(?,?)"); a += [int(ts), int(i)]
    lim = min(int(q.get("limit", 25)), 100)
    rows = c.execute(f"SELECT i.*,a.name assignee_name FROM items i LEFT JOIN users a ON a.id=i.assignee_id WHERE {' AND '.join(where)} "
                     "ORDER BY i.updated_at DESC,i.id DESC LIMIT ?", a + [lim + 1]).fetchall()
    more = len(rows) > lim; rows = rows[:lim]
    return 200, {"items": [public(r) for r in rows],
                 "next_cursor": f"{rows[-1]['updated_at']}:{rows[-1]['id']}" if more else None}

def summary(c, u):
    mine = "i.team_id IN (SELECT team_id FROM memberships WHERE user_id=?)"
    mgr = "i.team_id IN (SELECT team_id FROM memberships WHERE user_id=? AND role IN ('manager','admin'))"
    stale = now() - 3 * 86400000
    rows = c.execute(f"""SELECT i.*, CASE WHEN i.assignee_id=? AND i.status IN ('open','in_progress','review') THEN 'assigned to you'
        WHEN i.status='review' AND i.needs_approval=1 AND {mgr} THEN 'awaiting your approval'
        WHEN i.assignee_id IS NULL AND i.priority IN ('urgent','high') THEN 'unassigned & important'
        WHEN i.status IN ('open','in_progress') AND i.updated_at<? AND {mgr} THEN 'stale' END reason
        FROM items i WHERE {mine} AND i.status IN {ACTIVE} AND reason IS NOT NULL
        ORDER BY CASE i.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, i.updated_at LIMIT 30""",
        (u["id"], u["id"], stale, u["id"], u["id"])).fetchall()
    counts = {r[0]: r[1] for r in c.execute("SELECT status,COUNT(*) FROM items WHERE team_id IN (SELECT team_id FROM memberships WHERE user_id=?) GROUP BY status", (u["id"],))}
    return 200, {"attention": [public(r) for r in rows], "counts": counts}

def create_item(c, u, b):
    team = int(b.get("team_id") or 0); team_role(c, u, team, "create")
    pr = b.get("priority", "normal")
    if pr not in PRIORITIES: raise ApiError(422, "invalid priority")
    t = now()
    cur = c.execute("INSERT INTO items(team_id,title,description,priority,needs_approval,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (team, need_str(b, "title"), str(b.get("description", ""))[:5000], pr, 1 if b.get("needs_approval") else 0, u["id"], t, t))
    log(c, cur.lastrowid, u["id"], "created", {"priority": pr})
    return 201, public(c.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone())

def bump(c, item_id, sets, args, version=None):
    """Conditional update; if `version` given, it is the optimistic-concurrency check."""
    sql = f"UPDATE items SET {sets}, version=version+1, updated_at=? WHERE id=?"; a = args + [now(), item_id]
    if version is not None: sql += " AND version=?"; a.append(version)
    if c.execute(sql, a).rowcount == 0:
        raise ApiError(409, "item changed since you loaded it", current=public(c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()))
def need_version(b):
    if not isinstance(b.get("version"), int): raise ApiError(422, "version required")
    return b["version"]

def item_action(c, u, item_id, action, b):
    if action == "comment":
        it, _ = get_item(c, u, item_id, "comment")
        log(c, item_id, u["id"], "comment", {"body": need_str(b, "body", 5000)})
        return 201, {"ok": True}
    if action == "claim":
        it, _ = get_item(c, u, item_id, "claim")
        if it["status"] in ("done", "cancelled"): raise ApiError(422, "item is closed")
        # atomic compare-and-set: exactly one concurrent claimer can win
        n = c.execute("UPDATE items SET assignee_id=?,version=version+1,updated_at=? WHERE id=? AND assignee_id IS NULL", (u["id"], now(), item_id)).rowcount
        if n == 0:
            cur = c.execute("SELECT i.*,a.name assignee_name FROM items i LEFT JOIN users a ON a.id=i.assignee_id WHERE i.id=?", (item_id,)).fetchone()
            if cur["assignee_id"] == u["id"]: return 200, public(cur)   # idempotent re-claim
            raise ApiError(409, f"already claimed by {cur['assignee_name']}", current=public(cur))
        log(c, item_id, u["id"], "claimed", {"to": u["id"]})
    elif action == "assign":
        it, _ = get_item(c, u, item_id, "assign"); v = need_version(b); to = b.get("user_id")
        if to is not None and not c.execute("SELECT 1 FROM memberships WHERE user_id=? AND team_id=?", (to, it["team_id"])).fetchone():
            raise ApiError(422, "assignee must be a team member")
        bump(c, item_id, "assignee_id=?", [to], v)
        log(c, item_id, u["id"], "assigned", {"from": it["assignee_id"], "to": to})
    elif action == "transition":
        it, role = get_item(c, u, item_id, "comment"); v = need_version(b); to = b.get("to")
        if to not in TRANSITIONS.get(it["status"], []): raise ApiError(422, f"cannot go from {it['status']} to {to}")
        mgr = RANK[role] >= 3
        if not mgr and it["assignee_id"] != u["id"]: raise ApiError(403, "only the assignee or a manager can change status")
        if to == "done" and it["needs_approval"] and not mgr: raise ApiError(403, "approval by a manager required")
        if to == "in_progress" and it["assignee_id"] is None: raise ApiError(422, "claim the item first")
        bump(c, item_id, "status=?", [to], v)
        log(c, item_id, u["id"], "status", {"from": it["status"], "to": to})
    elif action == "update":
        it, role = get_item(c, u, item_id, "comment"); v = need_version(b); sets, args, ch = [], [], {}
        if not (RANK[role] >= 3 or u["id"] in (it["created_by"], it["assignee_id"])): raise ApiError(403, "not allowed to edit this item")
        for f in ("title", "description"):
            if f in b: sets.append(f"{f}=?"); args.append(need_str(b, f, 5000) if f == "title" else str(b[f])[:5000]); ch[f] = True
        if "priority" in b:
            if RANK[role] < PERMS["prioritize"]: raise ApiError(403, "only managers can change priority")
            if b["priority"] not in PRIORITIES: raise ApiError(422, "invalid priority")
            sets.append("priority=?"); args.append(b["priority"]); ch["priority"] = [it["priority"], b["priority"]]
        if not sets: raise ApiError(422, "nothing to update")
        bump(c, item_id, ",".join(sets), args, v); log(c, item_id, u["id"], "updated", ch)
    else: raise ApiError(404, "unknown action")
    return 200, public(c.execute("SELECT i.*,a.name assignee_name FROM items i LEFT JOIN users a ON a.id=i.assignee_id WHERE i.id=?", (item_id,)).fetchone())

def item_detail(c, u, item_id, q):
    it, role = get_item(c, u, item_id, "read")
    before = int(q.get("before", 1 << 60))
    ev = c.execute("SELECT e.*,u.name actor FROM events e LEFT JOIN users u ON u.id=e.actor_id WHERE item_id=? AND e.id<? ORDER BY e.id DESC LIMIT 50", (item_id, before)).fetchall()
    d = public(it); d["role"] = role
    d["assignee_name"] = (c.execute("SELECT name FROM users WHERE id=?", (it["assignee_id"],)).fetchone() or [None])[0]
    d["members"] = [public(r) for r in c.execute("SELECT u.id,u.name,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.team_id=?", (it["team_id"],))]
    return 200, {"item": d, "events": [{**public(e), "payload": json.loads(e["payload"])} for e in ev]}

def route(c, u, method, path, q, b):
    if method == "GET":
        if path == "/api/me":
            return 200, {"user": {k: u[k] for k in ("id", "name", "username")}, "teams": [public(r) for r in c.execute(
                "SELECT t.id,t.name,m.role FROM memberships m JOIN teams t ON t.id=m.team_id WHERE m.user_id=?", (u["id"],))]}
        if path == "/api/items": return list_items(c, u, q)
        if path == "/api/summary": return summary(c, u)
        if path == "/api/notifications":
            return 200, {"notifications": [public(r) for r in c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))]}
        m = re.fullmatch(r"/api/items/(\d+)", path)
        if m: return item_detail(c, u, int(m[1]), q)
    elif method == "POST":
        if path == "/api/items": return create_item(c, u, b)
        m = re.fullmatch(r"/api/items/(\d+)/(comment|claim|assign|transition|update)", path)
        if m: return item_action(c, u, int(m[1]), m[2], b)
    raise ApiError(404, "no such route")

def handle(method, path, q=None, token=None, headers=None, body=None):
    """Transport-independent entry point (used by HTTP server and tests)."""
    q, headers, body = q or {}, headers or {}, body or {}
    c = connect()
    try:
        if path == "/api/login" and method == "POST":   # demo auth: username -> bearer token
            r = c.execute("SELECT token FROM users WHERE username=?", (body.get("username"),)).fetchone()
            return (200, {"token": r[0]}) if r else (401, {"error": "unknown user"})
        u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone() if token else None
        if not u: return 401, {"error": "unauthenticated"}
        if method == "GET":
            return route(c, u, method, path, q, body)
        key = headers.get("idempotency-key")
        c.execute("BEGIN IMMEDIATE")
        try:
            if key:
                r = c.execute("SELECT status,body FROM idem WHERE user_id=? AND key=?", (u["id"], key)).fetchone()
                if r: c.execute("ROLLBACK"); return r[0], json.loads(r[1])
            st, out = route(c, u, method, path, q, body)
            if key: c.execute("INSERT INTO idem VALUES(?,?,?,?)", (u["id"], key, st, json.dumps(out)))
            c.execute("COMMIT"); return st, out
        except BaseException:
            c.execute("ROLLBACK"); raise
    except ApiError as e: return e.status, {"error": e.msg, **e.extra}
    except (ValueError, KeyError, TypeError) as e: return 400, {"error": f"bad request: {e}"}
    finally: c.close()

# ---------- async worker (transactional outbox) ----------
def process_outbox(limit=50):
    """At-least-once delivery; effects are idempotent via UNIQUE(event_id,user_id)."""
    c = connect(); done = 0
    for job in c.execute("SELECT * FROM outbox WHERE state='pending' AND run_after<=? ORDER BY id LIMIT ?", (now(), limit)).fetchall():
        try:
            if TEST_HOOKS["fail_notify"] > 0: TEST_HOOKS["fail_notify"] -= 1; raise RuntimeError("simulated failure")
            e = c.execute("SELECT e.*,u.name actor,i.title,i.assignee_id,i.created_by FROM events e JOIN items i ON i.id=e.item_id LEFT JOIN users u ON u.id=e.actor_id WHERE e.id=?", (job["event_id"],)).fetchone()
            p = json.loads(e["payload"]); who = {e["assignee_id"], e["created_by"], p.get("from")} - {None, e["actor_id"]}
            for uid in who:
                c.execute("INSERT OR IGNORE INTO notifications(event_id,user_id,item_id,text,created_at) VALUES(?,?,?,?,?)",
                          (e["id"], uid, e["item_id"], f"{e['actor']} {e['type']} on '{e['title']}'", now()))
            c.execute("UPDATE outbox SET state='done' WHERE id=?", (job["id"],)); done += 1
        except Exception as ex:
            n = job["attempts"] + 1
            c.execute("UPDATE outbox SET attempts=?,last_error=?,state=?,run_after=? WHERE id=?",
                      (n, str(ex), "dead" if n >= 5 else "pending", now() + min(2 ** n, 60) * 1000 if os.environ.get("BACKOFF", "1") == "1" else 0, job["id"]))
    c.close(); return done
def worker():
    while True:
        try: process_outbox()
        except Exception as e: print("worker error", e, file=sys.stderr)
        time.sleep(1)

# ---------- seed + HTTP ----------
def seed(n_items=3000):
    init_db(); c = connect()
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]: return
    users = [("alice", "Alice (admin)"), ("bob", "Bob (manager)"), ("carol", "Carol (member)"), ("dave", "Dave (member)"), ("erin", "Erin (viewer)")]
    for i, (un, nm) in enumerate(users, 1): c.execute("INSERT INTO users VALUES(?,?,?,?)", (i, un, nm, secrets.token_hex(16)))
    for i, nm in enumerate(["Support", "Engineering", "Finance"], 1): c.execute("INSERT INTO teams VALUES(?,?)", (i, nm))
    for m in [(1,1,"admin"),(1,2,"admin"),(1,3,"admin"),(2,1,"manager"),(2,2,"member"),(3,1,"member"),(3,2,"member"),(4,2,"member"),(4,3,"member"),(5,1,"viewer"),(5,3,"member")]:
        c.execute("INSERT INTO memberships VALUES(?,?,?)", m)
    t = now(); rows = []
    for i in range(n_items):
        team = random.choice([1, 2, 3]); mem = [m[0] for m in c.execute("SELECT user_id FROM memberships WHERE team_id=?", (team,))]
        st = random.choice(["open", "open", "in_progress", "review", "done"]); asg = None if st == "open" and random.random() < .7 else random.choice(mem)
        if st != "open" and not asg: asg = mem[0]
        ts = t - random.randint(0, 20 * 86400000)
        rows.append((team, f"{random.choice(['Payment failing for','Incident:','Compliance request','Investigate','Refund approval'])} #{i}", "Seeded item", st,
                     random.choice(PRIORITIES), asg, int(st == "review" and random.random() < .5), mem[0], ts, ts))
    c.executemany("INSERT INTO items(team_id,title,description,status,priority,assignee_id,needs_approval,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    c.close()

class H(BaseHTTPRequestHandler):
    def _do(self, method):
        u = urlparse(self.path)
        if method == "GET" and not u.path.startswith("/api/"):
            data = open(os.path.join(os.path.dirname(__file__), "static", "index.html"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(data); return
        body = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try: body = json.loads(self.rfile.read(n))
            except ValueError: body = None
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if body is None: st, out = 400, {"error": "invalid json"}
        else: st, out = handle(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, token, {k.lower(): v for k, v in self.headers.items()}, body)
        data = json.dumps(out).encode()
        self.send_response(st); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    do_GET = lambda s: s._do("GET")
    do_POST = lambda s: s._do("POST")
    def log_message(self, *a): pass

if __name__ == "__main__":
    seed(); threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", 8000)); print(f"http://localhost:{port}")
    ThreadingHTTPServer(("", port), H).serve_forever()
