import os
import random
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from flask import (
    Flask, render_template, request, redirect, session,
    url_for, abort, jsonify, flash
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# ---------------- App & DB config ----------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

database_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
# Heroku sometimes provides postgres:// which SQLAlchemy deprecates
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ---------------- Models ----------------
class ExperimentState(db.Model):
    __tablename__ = "experiment_state"
    id = db.Column(db.Integer, primary_key=True)
    is_open = db.Column(db.Boolean, default=True)
    title = db.Column(db.String(120), default="ECON 3310 Experiment 2")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Participant(db.Model):
    __tablename__ = "participants"
    id = db.Column(db.String, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # demographics
    name = db.Column(db.String(80))
    gender = db.Column(db.String(20))
    age = db.Column(db.Integer)
    race = db.Column(db.String(50))

    # experiment
    chosen_round = db.Column(db.Integer)   # R in {1..10}
    rounds = db.Column(db.JSON)            # list of dicts per round
    average_x = db.Column(db.Float)
    final_payoff = db.Column(db.Float)

    def to_row(self):
        """Flatten for CSV export."""
        row = {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "name": self.name,
            "gender": self.gender,
            "age": self.age,
            "race": self.race,
            "chosen_round": self.chosen_round,
            "average_x": self.average_x,
            "final_payoff": self.final_payoff,
        }
        if isinstance(self.rounds, list):
            for r in self.rounds:
                i = r.get("round")
                row[f"x_{i}"] = r.get("x")
                # Support both new (win) and old (flip) schemas
                win = r.get("win")
                if win is None:
                    win = (r.get("flip") == "heads")
                row[f"win_{i}"] = bool(win)
                row[f"wealth_{i}"] = r.get("wealth")
                row[f"time_ms_{i}"] = r.get("time_ms")
        return row


# ---------------- One-time setup & light migrations ----------------
def ensure_state():
    st = ExperimentState.query.get(1)
    if not st:
        st = ExperimentState(id=1)
        db.session.add(st)
        db.session.commit()
    return st


def run_simple_migrations():
    """
    Add columns that might be missing from older deployments
    (no Alembic required). Safe to run on every boot.
    """
    insp = db.inspect(db.engine)
    if "participants" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("participants")}
        with db.engine.begin() as conn:
            if "name" not in cols:
                conn.execute(text("ALTER TABLE participants ADD COLUMN name VARCHAR(80)"))
            if "average_x" not in cols:
                conn.execute(text("ALTER TABLE participants ADD COLUMN average_x DOUBLE PRECISION"))
            if "final_payoff" not in cols:
                conn.execute(text("ALTER TABLE participants ADD COLUMN final_payoff DOUBLE PRECISION"))
            if "rounds" not in cols:
                # Prefer JSON/JSONB; fall back to TEXT if needed
                try:
                    conn.execute(text("ALTER TABLE participants ADD COLUMN rounds JSONB"))
                except Exception:
                    try:
                        conn.execute(text("ALTER TABLE participants ADD COLUMN rounds JSON"))
                    except Exception:
                        conn.execute(text("ALTER TABLE participants ADD COLUMN rounds TEXT"))


with app.app_context():
    db.create_all()
    run_simple_migrations()
    ensure_state()


# ---------------- Session helpers ----------------
def require_pid():
    return session.get("pid")


def current_state():
    return session.setdefault(
        "state",
        {
            "rounds": [],      # list of dicts: {round, x, win, wealth, time_ms}
            "R": None,         # secret chosen round 1..10
            "demographics": {},  # name, gender, age, race
        },
    )


def experiment_open_required():
    return ensure_state().is_open


# ---------------- Player routes ----------------
@app.route("/")
def index():
    st = ensure_state()
    return render_template("index.html", open=st.is_open, title=st.title)


@app.route("/start", methods=["POST"])
def start():
    if not experiment_open_required():
        flash("The experiment is currently closed. Please contact the coordinator.")
        return redirect(url_for("index"))
    session.clear()
    session["pid"] = str(uuid4())
    st = current_state()
    st["R"] = random.randint(1, 10)  # secret round
    session["state"] = st
    return redirect(url_for("survey"))


@app.route("/survey", methods=["GET", "POST"])
def survey():
    if not require_pid():
        return redirect(url_for("index"))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip() or None
        gender = request.form.get("gender") or ""
        age = request.form.get("age") or ""
        race = request.form.get("race") or ""
        st = current_state()
        st["demographics"] = {
            "name": name,
            "gender": gender,
            "age": int(age) if age else None,
            "race": race,
        }
        session["state"] = st
        return redirect(url_for("instructions"))
    return render_template("survey.html")


@app.route("/instructions")
def instructions():
    if not require_pid():
        return redirect(url_for("index"))
    return render_template("instructions.html")


@app.route("/round/<int:n>", methods=["GET", "POST"])
def round_page(n: int):
    if not require_pid():
        return redirect(url_for("index"))
    if n < 1 or n > 10:
        abort(404)

    st = current_state()
    rounds = st["rounds"]

    if request.method == "POST":
        # Get decision
        try:
            x = int(request.form.get("x") or "0")
        except ValueError:
            x = 0
        x = max(0, min(100, x))
        time_ms = int(request.form.get("time_ms") or "0")

        # 50/50 outcome: win or not win
        win = random.choice([True, False])
        wealth = 100 - x + (2.5 * x if win else 0.0)

        # Save/overwrite round n
        found = False
        for r in rounds:
            if r["round"] == n:
                r.update({"x": x, "win": win, "wealth": wealth, "time_ms": time_ms})
                found = True
                break
        if not found:
            rounds.append({"round": n, "x": x, "win": win, "wealth": wealth, "time_ms": time_ms})

        rounds.sort(key=lambda r: r["round"])
        st["rounds"] = rounds
        session["state"] = st

        # Show per-round outcome
        return redirect(url_for("round_outcome", n=n))

    # GET -> prefill if player came back
    prev = next((r for r in rounds if r["round"] == n), None)
    prefill = prev["x"] if prev else 0
    return render_template("round.html", n=n, prefill=prefill)


@app.route("/round/<int:n>/outcome")
def round_outcome(n: int):
    if not require_pid():
        return redirect(url_for("index"))
    st = current_state()
    r = next((r for r in st["rounds"] if r["round"] == n), None)
    if not r:
        return redirect(url_for("round_page", n=n))
    next_url = url_for("round_page", n=n + 1) if n < 10 else url_for("results")
    return render_template("outcome.html", r=r, n=n, next_url=next_url)


@app.route("/results")
def results():
    if not require_pid():
        return redirect(url_for("index"))

    st = current_state()
    rounds = st.get("rounds", [])
    if len(rounds) < 10:
        # prevent skipping ahead
        return redirect(url_for("round_page", n=len(rounds) + 1))

    xs = [r.get("x", 0) for r in rounds]
    average_x = (sum(xs) / len(xs)) if xs else 0.0
    R = st.get("R") or 1
    wealth_R = next((r["wealth"] for r in rounds if r["round"] == R), 0.0)
    final_payoff = wealth_R

    # Persist to DB (idempotent upsert by session id)
    pid = require_pid()
    p = Participant.query.get(pid)
    if not p:
        p = Participant(id=pid)

    d = st.get("demographics", {})
    p.name = d.get("name")
    p.gender = d.get("gender")
    p.age = d.get("age")
    p.race = d.get("race")
    p.chosen_round = R
    p.rounds = rounds
    p.average_x = average_x
    p.final_payoff = final_payoff

    db.session.add(p)
    db.session.commit()

    return render_template(
        "results.html",
        R=R,
        rounds=rounds,
        average_x=average_x,
        final_payoff=final_payoff,
    )


# ---------------- Admin: dashboard, export & controls ----------------
def require_admin():
    key = request.args.get("key") or request.form.get("key") or ""
    expected = os.getenv("ADMIN_KEY")
    if not expected or key != expected:
        abort(403)


@app.route("/admin")
def admin_home():
    require_admin()
    st = ensure_state()
    counts = {
        "participants": Participant.query.count(),
        "decisions": sum(len(p.rounds or []) for p in Participant.query.all()),
    }
    return render_template("admin.html", state=st, counts=counts, key=request.args.get("key"))


@app.route("/admin/state", methods=["POST"])
def admin_state():
    require_admin()
    st = ensure_state()
    action = request.form.get("action")
    if action == "open":
        st.is_open = True
    elif action == "close":
        st.is_open = False
    db.session.add(st)
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form.get("key")))


@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    require_admin()
    Participant.query.delete()
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form.get("key")))


@app.route("/admin/export")
def admin_export():
    require_admin()
    import csv, io

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "id",
            "created_at",
            "name",
            "gender",
            "age",
            "race",
            "chosen_round",
            "average_x",
            "final_payoff",
            *[f"x_{i}" for i in range(1, 11)],
            *[f"win_{i}" for i in range(1, 11)],
            *[f"wealth_{i}" for i in range(1, 11)],
            *[f"time_ms_{i}" for i in range(1, 11)],
        ],
    )
    writer.writeheader()
    for p in Participant.query.order_by(Participant.created_at.asc()).all():
        writer.writerow(p.to_row())

    buf.seek(0)
    from flask import Response

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=participants.csv"},
    )


@app.route("/admin/stats.json")
def admin_stats_json():
    """Aggregated stats for dashboard charts."""
    require_admin()

    decisions = []
    for p in Participant.query.all():
        for r in (p.rounds or []):
            decisions.append(
                {
                    "pid": p.id,
                    "name": p.name or p.id[:6],
                    "gender": p.gender or "Unspecified",
                    "age": p.age or None,
                    "race": p.race or "Unspecified",
                    "round": r.get("round"),
                    "x": r.get("x", 0),
                    "win": bool(r.get("win", (r.get("flip") == "heads"))),
                    "wealth": r.get("wealth", 0.0),
                }
            )

    # Histograms by bin size
    def hist_by_bin(bin_size):
        from collections import defaultdict as DD

        bins = DD(int)
        for d in decisions:
            b = (d["x"] // bin_size) * bin_size
            key = f"{int(b)}–{int(b + bin_size - 1)}"
            bins[key] += 1

        def low(k):  # sort by numeric lower bound
            return int(k.split("–")[0])

        return [{"bin": k, "count": bins[k]} for k in sorted(bins.keys(), key=low)]

    # Average x by group helper
    def avg_by(key):
        sums, counts = {}, {}
        for d in decisions:
            g = d[key]
            sums[g] = sums.get(g, 0) + d["x"]
            counts[g] = counts.get(g, 0) + 1
        groups = sorted(counts.keys())
        return [{"group": g, "avg_x": (sums[g] / counts[g]) if counts[g] else 0.0} for g in groups]

    # Names per 10-pt interval
    name_bins = defaultdict(list)
    for d in decisions:
        b = (d["x"] // 10) * 10
        key = f"{int(b)}–{int(b + 9)}"
        name_bins[key].append(d["name"])
    names_by_bin = [
        {"bin": k, "names": sorted(set(v))}
        for k, v in sorted(name_bins.items(), key=lambda kv: int(kv[0].split("–")[0]))
    ]

    return jsonify(
        {
            "hist_5": hist_by_bin(5),
            "hist_10": hist_by_bin(10),
            "hist_20": hist_by_bin(20),
            "avg_by_gender": avg_by("gender"),
            "avg_by_age": avg_by("age"),   # bucketed on the frontend if desired
            "avg_by_race": avg_by("race"),
            "names_by_bin_10": names_by_bin,
            "decision_count": len(decisions),
            "participant_count": Participant.query.count(),
        }
    )


# ---------------- Health ----------------
@app.route("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}


# ---------------- Entrypoint ----------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

