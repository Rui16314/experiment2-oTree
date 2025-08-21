# ECON3310 Risky Investment Game (Flask)

A minimal Flask app for a 10-round single-person decision experiment used in ECON 3310.
Each round: start with 100 points, choose investment x in a risky asset, flip a fair coin:
- Heads: wealth = 100 - x + 2.5x
- Tails: wealth = 100 - x

At the beginning of each session a secret round R is chosen. Final payoff = wealth in round R.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export SECRET_KEY="dev-change-me"
python app.py
# visit http://127.0.0.1:5000
```

The app auto-creates the database tables on first run.

## Deploy to Heroku

> You need a Heroku account and the Heroku CLI installed.

```bash
# 1) Create a new Git repo (or use GitHub first, then connect)
git init
git add .
git commit -m "Initial commit"

# 2) Create the Heroku app
heroku login
heroku create econ3310-game  # or let Heroku pick a name

# 3) Add the Python buildpack (usually automatic)
heroku buildpacks:add heroku/python

# 4) Add a Postgres database (choose a plan available to you, e.g., hobby-dev/mini)
heroku addons:create heroku-postgresql:mini

# 5) Set secrets
heroku config:set SECRET_KEY="$(python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY)"
heroku config:set ADMIN_KEY="some-shared-export-key"

# 6) Deploy
git push heroku HEAD:main  # or: git push heroku main
# If your default branch is 'master', use HEAD:master instead

# 7) Open
heroku open
```

### Export data
Download a CSV of all participants (guarded by `ADMIN_KEY`):

```
https://<your-app>.herokuapp.com/admin/export?key=YOUR_ADMIN_KEY
```

## Repo structure
```
.
├── app.py
├── Procfile
├── requirements.txt
├── runtime.txt
├── static/
│   └── styles.css
└── templates/
    ├── base.html
    ├── index.html
    ├── instructions.html
    ├── round.html
    └── results.html
```

## Notes
- Heroku has an ephemeral filesystem; do not rely on files for storage. This project uses Postgres via SQLAlchemy.
- To run on another provider (Railway, Render, etc.) the same setup works: provide `DATABASE_URL`, `SECRET_KEY`, and a `PORT` if required by the host.
