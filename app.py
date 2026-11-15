"""Event and Club Management. Every SQL query lives in sql/app_queries.sql.
The website connects to PostgreSQL AS the role you choose, so the database decides what is allowed."""
import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from markupsafe import escape

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-secret")

# PostgreSQL login role -> label shown on the website (roles are created in sql/03_roles.sql)
ROLES = {"admin_user": "Admin", "club_admin_user": "Club admin", "student_user": "Student"}


def load_queries(path):
    """Read 'name -> SQL text' pairs from the .sql file."""
    queries, name, lines = {}, None, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("-- name:"):
                if name:
                    queries[name] = "".join(lines).strip()
                name, lines = line.split(":", 1)[1].strip(), []
            elif name:
                lines.append(line)
    if name:
        queries[name] = "".join(lines).strip()
    return queries


Q = load_queries(os.path.join(app.root_path, "sql", "app_queries.sql"))


def db():
    if "db" not in g:
        g.db = psycopg.connect(
            host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "event_db"), user=session["role"],
            password=os.environ["ROLE_PASSWORD"], row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(error):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def error_text(err):
    d = err.diag
    return " ".join(x for x in (d.message_primary, d.message_detail) if x) or str(err)


@app.errorhandler(psycopg.OperationalError)
def cannot_connect(err):
    return ("<h3>Cannot connect to the database</h3><p>" + str(escape(error_text(err))) +
            "</p><p>Check: PostgreSQL is running, the .env file is correct, "
            "and sql/03_roles.sql was run.</p>"), 500


def fetch(name, params=None):
    with db().cursor() as cur:
        cur.execute(Q[name], params)
        return cur.fetchall()


def fetch_safe(name, params=None):
    """Like fetch, but a database error (for example 'permission denied') is shown as a message."""
    try:
        return fetch(name, params)
    except psycopg.Error as err:
        db().rollback()
        flash(error_text(err), "error")
        return []


def execute(name, params=None):
    """Run a write query, commit, and return how many rows it changed."""
    try:
        with db().cursor() as cur:
            cur.execute(Q[name], params)
            count = cur.rowcount
        db().commit()
        return count
    except psycopg.Error:
        db().rollback()
        raise


def F(name, label, type="text", opt=None, req=True, on="both"):
    """A form field. on = 'both', 'new' (only when adding), or 'edit' (only when editing)."""
    return dict(name=name, label=label, type=type, opt=opt, req=req, on=on)


ENT = {
    "students": dict(title="Students", pk="student_id",
        cols=["student_id", "name", "email", "phone", "department", "year", "cgpa"],
        fields=[F("name", "Name"), F("email", "Email", "email"), F("phone", "Phone", req=False),
                F("department", "Department"), F("year", "Year (1-4)", "number"),
                F("cgpa", "CGPA", "number", req=False)]),
    "clubs": dict(title="Clubs", pk="club_id",
        cols=["club_id", "name", "faculty_advisor", "member_count", "event_count"],
        fields=[F("name", "Name"), F("description", "Description", req=False),
                F("email", "Email", "email", req=False), F("faculty_advisor", "Faculty advisor", req=False)]),
    "venues": dict(title="Venues", pk="venue_id",
        cols=["venue_id", "name", "location", "capacity"],
        fields=[F("name", "Name"), F("location", "Location"), F("capacity", "Capacity", "number")]),
    "events": dict(title="Events", pk="event_id",
        cols=["event_id", "title", "club", "venue", "start_date", "status", "capacity", "seats_taken", "seats_left"],
        reject="Not saved. The capacity must not be above the venue capacity or below the seats already taken, "
               "and the venue must be free at that time.",
        fields=[F("title", "Title"), F("description", "Description", req=False),
                F("start_date", "Start", "datetime-local"), F("end_date", "End", "datetime-local"),
                F("capacity", "Capacity", "number"),
                F("status", "Status", "select", ["scheduled", "completed", "cancelled"]),
                F("club_id", "Club", "select", "clubs_options"),
                F("venue_id", "Venue", "select", "venues_options")]),
    "club_members": dict(title="Club members", pk="club_member_id",
        cols=["club_member_id", "student", "club", "join_date"],
        fields=[F("student_id", "Student", "select", "students_options"),
                F("club_id", "Club", "select", "clubs_options")]),
    "registrations": dict(title="Registrations", pk="registration_id",
        cols=["registration_id", "student", "event", "registered_on", "status"],
        reject="Not saved. The event is full, or it is not an upcoming scheduled event.",
        fields=[F("student_id", "Student", "select", "students_options", on="new"),
                F("event_id", "Event", "select", "events_options", on="new"),
                F("status", "Status", "select", ["registered", "cancelled", "attended"], on="edit")]),
    "expenses": dict(title="Expenses", pk="expense_id",
        cols=["expense_id", "event", "description", "amount", "expense_date"],
        fields=[F("event_id", "Event", "select", "events_options"), F("description", "Description"),
                F("amount", "Amount", "number"), F("expense_date", "Date", "date")]),
}

REPORTS = [("report_registrations", "Registrations per event"),
           ("report_clubs", "Club activity"),
           ("report_expenses_event", "Expenses per event"),
           ("report_expenses_club", "Expenses per club"),
           ("report_expenses_month", "Expenses per month"),
           ("report_top_students", "Most active students"),
           ("report_departments", "Students per department")]


@app.context_processor
def inject_globals():
    return {"nav": [(key, e["title"]) for key, e in ENT.items()],
            "current_role": ROLES.get(session.get("role"))}


@app.before_request
def need_role():
    if request.endpoint in ("login", "static"):
        return None
    if session.get("role") not in ROLES:
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST" and request.form.get("role") in ROLES:
        session["role"] = request.form["role"]
        return redirect(url_for("index"))
    return render_template("login.html", roles=ROLES)


def entity(ent):
    if ent not in ENT:
        abort(404)
    return ENT[ent]


def fields_for(e, editing):
    return [f for f in e["fields"] if f["on"] in ("both", "edit" if editing else "new")]


def options_for(fields):
    out = {}
    for f in fields:
        if f["type"] == "select":
            if isinstance(f["opt"], str):
                out[f["name"]] = [(r["id"], r["label"]) for r in fetch_safe(f["opt"])]
            else:
                out[f["name"]] = [(x, x) for x in f["opt"]]
    return out


@app.get("/")
def index():
    stats = (fetch_safe("home_stats") or [{}])[0]
    return render_template("index.html", stats=stats, sql=Q["home_stats"])


@app.get("/reports")
def reports():
    data = []
    for name, title in REPORTS:
        try:
            rows, error = fetch(name), None
        except psycopg.Error as err:
            db().rollback()
            rows, error = [], error_text(err)
        data.append((title, rows, Q[name], error))
    return render_template("reports.html", data=data)


@app.get("/<ent>")
def listing(ent):
    e = entity(ent)
    q = request.args.get("q", "")
    rows = fetch_safe(f"{ent}_list", {"q": f"%{q}%"})
    return render_template("list.html", ent=ent, e=e, rows=rows, q=q, sql=Q[f"{ent}_list"])


def handle_form(ent, row_id=None):
    e = entity(ent)
    editing = row_id is not None
    fields = fields_for(e, editing)
    values = {}
    if editing:
        found = fetch_safe(f"{ent}_get", {"id": row_id})
        if not found:
            abort(404)
        values = found[0]
    if request.method == "POST":
        data = {f["name"]: request.form.get(f["name"], "").strip() or None for f in fields}
        values = data
        try:
            query = f"{ent}_update" if editing else f"{ent}_insert"
            if execute(query, {**data, "id": row_id}):
                flash("Saved.", "ok")
                return redirect(url_for("listing", ent=ent))
            flash(e.get("reject", "Nothing was saved."), "error")
        except psycopg.Error as err:
            flash(error_text(err), "error")
    return render_template("form.html", ent=ent, e=e, fields=fields, values=values,
                           editing=editing, options=options_for(fields))


@app.route("/<ent>/new", methods=["GET", "POST"])
def new_form(ent):
    return handle_form(ent)


@app.route("/<ent>/<int:row_id>/edit", methods=["GET", "POST"])
def edit_form(ent, row_id):
    return handle_form(ent, row_id)


@app.post("/<ent>/<int:row_id>/delete")
def delete(ent, row_id):
    entity(ent)
    try:
        execute(f"{ent}_delete", {"id": row_id})
        flash("Deleted.", "ok")
    except psycopg.Error as err:
        flash(error_text(err), "error")
    return redirect(url_for("listing", ent=ent))


if __name__ == "__main__":
    app.run(debug=True)