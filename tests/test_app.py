import os, tempfile, threading, unittest
os.environ["BACKOFF"] = "0"
from newtonite import app

def post(tok, path, body=None, key=None):
    return app.handle("POST", path, token=tok, body=body or {}, headers={"idempotency-key": key} if key else {})

class T(unittest.TestCase):
    def setUp(self):
        app.DB = os.path.join(tempfile.mkdtemp(), "t.db"); app.seed(0)
        c = app.connect(); self.tok = {r["username"]: r["token"] for r in c.execute("SELECT * FROM users")}; c.close()
        self.alice, self.bob, self.carol, self.dave, self.erin = (self.tok[n] for n in ("alice", "bob", "carol", "dave", "erin"))
    def item(self, tok=None, **kw):
        st, it = post(tok or self.carol, "/api/items", {"team_id": 1, "title": "x", **kw}); self.assertEqual(st, 201); return it

    def test_concurrent_claim_has_exactly_one_winner(self):
        it = self.item(); res = []
        ts = [threading.Thread(target=lambda t=t: res.append(post(t, f"/api/items/{it['id']}/claim")[0])) for t in (self.alice, self.bob, self.carol) * 4]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertIn(409, res)
        c = app.connect(); winner = c.execute("SELECT assignee_id FROM items WHERE id=?", (it["id"],)).fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM events WHERE item_id=? AND type='claimed'", (it["id"],)).fetchone()[0]
        self.assertIsNotNone(winner); self.assertEqual(n, 1)   # one claim event; losers got 409

    def test_stale_version_rejected(self):
        it = self.item(); v = it["version"]
        self.assertEqual(post(self.bob, f"/api/items/{it['id']}/update", {"version": v, "title": "new"})[0], 200)
        st, out = post(self.bob, f"/api/items/{it['id']}/update", {"version": v, "title": "stale"})
        self.assertEqual(st, 409); self.assertEqual(out["current"]["title"], "new")

    def test_idempotent_create(self):
        a = post(self.carol, "/api/items", {"team_id": 1, "title": "dup"}, key="k1"); b = post(self.carol, "/api/items", {"team_id": 1, "title": "dup"}, key="k1")
        self.assertEqual(a, b); c = app.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_authorization_enforced_server_side(self):
        it = self.item()
        self.assertEqual(post(self.erin, f"/api/items/{it['id']}/comment", {"body": "hi"})[0], 403)          # viewer
        self.assertEqual(post(self.dave, f"/api/items/{it['id']}/comment", {"body": "hi"})[0], 404)          # not in team
        self.assertEqual(app.handle("GET", f"/api/items/{it['id']}", token=self.dave)[0], 404)
        self.assertEqual(post(self.carol, f"/api/items/{it['id']}/update", {"version": 1, "priority": "urgent"})[0], 403)  # member
        self.assertEqual(post(self.bob, f"/api/items/{it['id']}/update", {"version": 1, "priority": "urgent"})[0], 200)   # manager
        self.assertEqual(app.handle("GET", "/api/items", token=self.dave)[1]["items"], [])

    def test_workflow_and_approval_rules(self):
        it = self.item(needs_approval=True); i = it["id"]
        self.assertEqual(post(self.bob, f"/api/items/{i}/transition", {"version": 1, "to": "in_progress"})[0], 422)  # unclaimed
        _, it = post(self.carol, f"/api/items/{i}/claim"); v = it["version"]
        self.assertEqual(post(self.carol, f"/api/items/{i}/transition", {"version": v, "to": "done"})[0], 422)         # skip states
        for to in ("in_progress", "review"):
            st, it = post(self.carol, f"/api/items/{i}/transition", {"version": v, "to": to}); self.assertEqual(st, 200); v = it["version"]
        self.assertEqual(post(self.carol, f"/api/items/{i}/transition", {"version": v, "to": "done"})[0], 403)         # needs manager
        self.assertEqual(post(self.bob, f"/api/items/{i}/transition", {"version": v, "to": "done"})[0], 200)
        self.assertEqual(post(self.bob, f"/api/items/{i}/transition", {"version": v + 1, "to": "open"})[0], 422)      # terminal

    def test_outbox_retries_and_is_idempotent(self):
        it = self.item(); post(self.bob, f"/api/items/{it['id']}/assign", {"version": it["version"], "to": 1, "user_id": 3})
        app.TEST_HOOKS["fail_notify"] = 1
        app.process_outbox(); app.process_outbox(); app.process_outbox()   # first run fails, retries succeed
        c = app.connect(); self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox WHERE state!='done'").fetchone()[0], 0)
        n = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        c.execute("UPDATE outbox SET state='pending'"); app.process_outbox()  # redelivery -> no duplicates
        self.assertEqual(c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], n)

    def test_pagination_stable(self):
        for _ in range(7): self.item()
        st, p1 = app.handle("GET", "/api/items", q={"limit": "3"}, token=self.carol)
        st, p2 = app.handle("GET", "/api/items", q={"limit": "3", "cursor": p1["next_cursor"]}, token=self.carol)
        self.assertFalse({x["id"] for x in p1["items"]} & {x["id"] for x in p2["items"]})

if __name__ == "__main__": unittest.main()
