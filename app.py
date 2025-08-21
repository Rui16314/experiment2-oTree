import os
import random
from datetime import datetime
from uuid import uuid4

from flask import Flask, render_template, request, redirect, session, url_for, abort, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB

# --- Config ---
app = Flask(__name__)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["SECRET_KEY"] = SECRET_KEY

database_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
# Heroku used to provide postgres://; SQLAlchemy prefers postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --- Models ---
class Participant(db.Model):
    __tablename__ = "participants"
    id = db.Column(db.String, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # demographics
    gender = db.Column(db.String(20))
    age = db.Column(db.Integer)
    race = db.Column(db.String(50))

    # experiment data
    chosen_round = db.Column(db.Integer)      # R in {1..10}
    rounds = db.Column(db.JSON)               # list of dicts: {round,x,flip,wealth,time_ms}
    average_x = db.Column(db.Float)
    final_payoff = db.Column(db.Float)

    def to_row(self):
        # Flatten for CSV export
        row = {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
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

with app.app_context():
    db.create_all()

# --- Helpers ---
def require_pid():
    pid = session.get("pid")
    if not pid:
        return None
    return pid

def current_state():
    return session.setdefault("state", {
        "rounds": [],   # list of dicts with x, flip, wealth
        "R": None,      # chosen round 1..10 (kept secret until the end)
        "demographics": {},
    })

# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start", methods=["POST"])
def start():
    # New participant
    session.clear()
    pid = str(uuid4())
    session["pid"] = pid
    st = current_state()
    st["R"] = random.randint(1,10)  # secret chosen round
    session["state"] = st
    return redirect(url_for("survey"))

@app.route("/survey", methods=["GET","POST"])
def survey():
    if not require_pid():
        return redirect(url_for("index"))
    if request.method == "POST":
        gender = request.form.get("gender") or ""
        age = request.form.get("age") or ""
        race = request.form.get("race") or ""
        st = current_state()
        st["demographics"] = {"gender": gender, "age": int(age) if age else None, "race": race}
        session["state"] = st
        return redirect(url_for("instructions"))
    return render_template("survey.html")

@app.route("/instructions")
def instructions():
    if not require_pid():
        return redirect(url_for("index"))
    return render_template("instructions.html")

@app.route("/round/<int:n>", methods=["GET","POST"])
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
        x = max(0, min(100, x))  # clamp to [0,100]

        time_ms = int(request.form.get("time_ms") or "0")

        # Flip a fair coin
        flip = random.choice(["heads", "tails"])
        wealth = 100 - x + (2.5 * x if flip == "heads" else 0.0)

        # Save this round
        found = False
        for r in rounds:
            if r["round"] == n:
                r.update({"x": x, "flip": flip, "wealth": wealth, "time_ms": time_ms})
                found = True
                break
        if not found:
            rounds.append({"round": n, "x": x, "flip": flip, "wealth": wealth, "time_ms": time_ms})

        # Sort by round number
        rounds.sort(key=lambda r: r["round"])
        st["rounds"] = rounds
        session["state"] = st

        # Continue
        if n < 10:
            return redirect(url_for("round_page", n=n+1))
        else:
            return redirect(url_for("results"))

    # GET
    # If already answered this round, prefill
    prev = next((r for r in rounds if r["round"] == n), None)
    prefill = prev["x"] if prev else 0
    return render_template("round.html", n=n, prefill=prefill)

@app.route("/results")
def results():
    if not require_pid():
        return redirect(url_for("index"))
    st = current_state()
    rounds = st.get("rounds", [])
    if len(rounds) < 10:
        # Protect against skipping ahead
        return redirect(url_for("round_page", n=len(rounds)+1))

    # Compute average x
    xs = [r.get("x", 0) for r in rounds]
    average_x = sum(xs) / len(xs) if xs else 0.0
    R = st.get("R") or 1
    wealth_R = next((r["wealth"] for r in rounds if r["round"] == R), 0.0)
    final_payoff = wealth_R

    # Persist to DB (idempotent)
    pid = require_pid()
    p = Participant.query.get(pid)
    if not p:
        p = Participant(id=pid)
    p.gender = st.get("demographics", {}).get("gender")
    p.age = st.get("demographics", {}).get("age")
    p.race = st.get("demographics", {}).get("race")
    p.chosen_round = R
    p.rounds = rounds
    p.average_x = average_x
    p.final_payoff = final_payoff
    db.session.add(p)
    db.session.commit()

    return render_template("results.html", R=R, rounds=rounds, average_x=average_x, final_payoff=final_payoff)

@app.route("/admin/export")
def admin_export():
    # Simple CSV export guarded by a shared key
    key = request.args.get("key", "")
    expected = os.getenv("ADMIN_KEY")
    if not expected or key != expected:
        abort(403)

    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "id","created_at","gender","age","race","chosen_round",
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
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=participants.csv"}
    )

# Health check
@app.route("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
