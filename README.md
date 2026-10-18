# Newtonite Ops

Internal web app for coordinating operational work: work items, ownership, approvals, history, notifications.
Python 3.9+ standard library + SQLite. **No dependencies to install.**

## Run
```bash
python -m newtonite        # or: make run
```
Open http://localhost:8000. First start seeds 3,000 items and 5 demo users
(alice=admin, bob=manager, carol/dave=member, erin=viewer). `make reset` wipes the DB.
Env vars: `PORT`, `DB_PATH`.

## Test
```bash
python -m unittest discover -s tests -t . -v      # or: make test
```

## Structure
```
newtonite/app.py            API, authorization, workflow rules, outbox worker
newtonite/__main__.py       entry point (python -m newtonite)
newtonite/static/index.html single-file UI
tests/test_app.py           concurrency, authz, workflow, idempotency, outbox tests
docs/ENGINEERING_DECISIONS.md
.github/workflows/ci.yml    runs tests on every push
.vscode/                    run/debug + test config
```

## API (all JSON, `Authorization: Bearer <token>`)
| Method | Path | Notes |
|---|---|---|
| POST | /api/login | `{username}` → token (demo auth) |
| GET | /api/me, /api/summary, /api/notifications | |
| GET | /api/items?status=&priority=&assignee=me\|none&q=&cursor=&limit= | keyset pagination |
| POST | /api/items | create; supports `Idempotency-Key` |
| GET | /api/items/{id} | item + history |
| POST | /api/items/{id}/claim · assign · transition · update · comment | `version` required where state is edited |

## Known limitations
- Demo auth only (no passwords/SSO).
- Search uses `LIKE`; use FTS5/Postgres full-text at scale.
- 5s polling instead of push; SQLite single writer; in-process worker; in-app notifications only.
- No rate limiting or CSRF/CORS hardening.

See [docs/ENGINEERING_DECISIONS.md](docs/ENGINEERING_DECISIONS.md).
