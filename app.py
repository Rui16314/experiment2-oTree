
import os
import random
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from flask import Flask, render_template, request, redirect, session, url_for, abort, jsonify, flash
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["SECRET_KEY"] = SECRET_KEY

database_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ----------------- Models -----------------
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

    name = db.Column(db.String(80))
    gender = db.Column(db.String(20))
    age = db.Column(db.Integer)
    race = db.Column(db.String(50))

    chosen_round = db.Column(db.Integer)
    rounds = db.Column(db.JSON)          # list of dicts
    average_x = db.Column(db.Float)
    final_payoff = db.Column(db.Float)

    def to_row(self):
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
                idx = r.get("round")
                row[f"x_{idx}"] = r.get("x")
                row[f"flip_{idx}"] = r.get("flip")
                row[f"wealth_{idx}"] = r.get("wealth")
                row[f"time_ms_{idx}"] = r.get("time_ms")
        return row

def ensure_state():
    st = ExperimentState.query.get(1)
    if not st:
        st = ExperimentState(id=1)
        db.session.add(st)
        db.session.commit()
    return st

with app.app_context():
    db.create_all()
    ensure_state()
def run_simple_migrations():
    """Add columns that might be missing from older deployments."""
    insp = db.inspect(db.engine)
    if "participants" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("participants")}
        with db.engine.begin() as conn:
            if "name" not in cols:
                conn.execute(text("ALTER TABLE participants ADD COLUMN name VARCHAR(80)"))

# --------------- Helpers -------------------
def require_pid():
    return session.get("pid")

def current_state():
    return session.setdefault("state", {
        "rounds": [],
        "R": None,
        "demographics": {},
    })

def experiment_open_required():
    return ensure_state().is_open

# --------------- Routes --------------------
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
    pid = str(uuid4())
    session["pid"] = pid
    st = current_state()
    st["R"] = random.randint(1, 10)
    session["state"] = st
    return redirect(url_for("survey"))

@app.route("/survey", methods=["GET", "POST"])
def survey():
    if not require_pid():
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name") or ""
        gender = request.form.get("gender") or ""
        age = request.form.get("age") or ""
        race = request.form.get("race") or ""
        st = current_state()
        st["demographics"] = {
            "name": name.strip() or None,
            "gender": gender,
            "age": int(age) if age else None,
            "race": race
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
def round_page(n):
    if not require_pid():
        return redirect(url_for("index"))
    if n < 1 or n > 10:
        abort(404)
    st = current_state()
    rounds = st["rounds"]

    if request.method == "POST":
        try:
            x = int(request.form.get("x") or "0")
        except ValueError:
            x = 0
        x = max(0, min(100, x))
        time_ms = int(request.form.get("time_ms") or "0")

        flip = random.choice(["heads", "tails"])
        wealth = 100 - x + (2.5 * x if flip == "heads" else 0.0)

        # Save
        found = False
        for r in rounds:
            if r["round"] == n:
                r.update({"x": x, "flip": flip, "wealth": wealth, "time_ms": time_ms})
                found = True
                break
        if not found:
            rounds.append({"round": n, "x": x, "flip": flip, "wealth": wealth, "time_ms": time_ms})
        rounds.sort(key=lambda r: r["round"])
        st["rounds"] = rounds
        session["state"] = st
        return redirect(url_for("round_outcome", n=n))

    prev = next((r for r in rounds if r["round"] == n), None)
    prefill = prev["x"] if prev else 0
    return render_template("round.html", n=n, prefill=prefill)

@app.route("/round/<int:n>/outcome")
def round_outcome(n):
    if not require_pid():
        return redirect(url_for("index"))
    st = current_state()
    r = next((r for r in st["rounds"] if r["round"] == n), None)
    if not r:
        return redirect(url_for("round_page", n=n))
    next_url = url_for("round_page", n=n+1) if n < 10 else url_for("results")
    return render_template("outcome.html", r=r, n=n, next_url=next_url)

@app.route("/results")
def results():
    if not require_pid():
        return redirect(url_for("index"))
    st = current_state()
    rounds = st.get("rounds", [])
    if len(rounds) < 10:
        return redirect(url_for("round_page", n=len(rounds)+1))

    xs = [r.get("x", 0) for r in rounds]
    average_x = sum(xs) / len(xs) if xs else 0.0
    R = st.get("R") or 1
    wealth_R = next((r["wealth"] for r in rounds if r["round"] == R), 0.0)
    final_payoff = wealth_R

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

    return render_template("results.html", R=R, rounds=rounds, average_x=average_x, final_payoff=final_payoff)

# ---------------- Admin: Dashboard & API ----------------
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
        "decisions": sum(len(p.rounds or []) for p in Participant.query.all())
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
    writer = csv.DictWriter(buf, fieldnames=[
        "id","created_at","name","gender","age","race","chosen_round",
        "average_x","final_payoff",
        *[f"x_{i}" for i in range(1,11)],
        *[f"flip_{i}" for i in range(1,11)],
        *[f"wealth_{i}" for i in range(1,11)],
        *[f"time_ms_{i}" for i in range(1,11)]
    ])
    writer.writeheader()
    for p in Participant.query.order_by(Participant.created_at.asc()).all():
        writer.writerow(p.to_row())
    buf.seek(0)
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=participants.csv"})

@app.route("/admin/stats.json")
def admin_stats_json():
    require_admin()
    decisions = []
    for p in Participant.query.all():
        for r in (p.rounds or []):
            decisions.append({
                "pid": p.id,
                "name": p.name or p.id[:6],
                "gender": p.gender or "Unspecified",
                "age": p.age or None,
                "race": p.race or "Unspecified",
                "round": r.get("round"),
                "x": r.get("x", 0),
                "flip": r.get("flip"),
                "wealth": r.get("wealth", 0.0),
            })

    def hist_by_bin(bin_size):
        bins = defaultdict(int)
        for d in decisions:
            b = (d["x"] // bin_size) * bin_size
            key = f"{int(b)}–{int(b+bin_size-1)}"
            bins[key] += 1
        def low(k): return int(k.split("–")[0])
        return [{"bin": k, "count": bins[k]} for k in sorted(bins.keys(), key=low)]

    # Average x by gender
    sums = defaultdict(int); counts = defaultdict(int)
    for d in decisions:
        g = d["gender"]
        sums[g] += d["x"]; counts[g] += 1
    avg_by_gender = [{"group": g, "avg_x": (sums[g]/counts[g]) if counts[g] else 0.0}
                     for g in sorted(counts.keys())]

    # Age buckets
    def age_bucket(a):
        if a is None: return "Unknown"
        if a < 20: return "<20"
        if a < 25: return "20–24"
        if a < 30: return "25–29"
        if a < 40: return "30–39"
        return "40+"
    sums = defaultdict(int); counts = defaultdict(int)
    for d in decisions:
        b = age_bucket(d["age"])
        sums[b] += d["x"]; counts[b] += 1
    avg_by_age = [{"group": g, "avg_x": (sums[g]/counts[g]) if counts[g] else 0.0}
                  for g in sorted(counts.keys())]

    # Average x by race
    sums = defaultdict(int); counts = defaultdict(int)
    for d in decisions:
        r = d["race"]
        sums[r] += d["x"]; counts[r] += 1
    avg_by_race = [{"group": g, "avg_x": (sums[g]/counts[g]) if counts[g] else 0.0}
                   for g in sorted(counts.keys())]

    name_bins = defaultdict(list)
    for d in decisions:
        b = (d["x"] // 10) * 10
        key = f"{int(b)}–{int(b+9)}"
        name_bins[key].append(d["name"])
    names_by_bin = [{"bin": k, "names": sorted(set(v))} for k, v in sorted(name_bins.items(), key=lambda kv: int(kv[0].split('–')[0]))]

    return jsonify({
        "hist_5": hist_by_bin(5),
        "hist_10": hist_by_bin(10),
        "hist_20": hist_by_bin(20),
        "avg_by_gender": avg_by_gender,
        "avg_by_age": avg_by_age,
        "avg_by_race": avg_by_race,
        "names_by_bin_10": names_by_bin,
        "decision_count": len(decisions),
        "participant_count": Participant.query.count(),
    })

@app.route("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
