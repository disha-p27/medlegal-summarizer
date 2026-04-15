from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pypdf import PdfReader, PdfWriter
from docx import Document
import requests
import sqlite3
from pathlib import Path
from datetime import datetime
import pytesseract
from pdf2image import convert_from_path
import re

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "app.db"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b"

app = Flask(__name__)
app.secret_key = "secret"

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

# ---------------- DATABASE ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password_hash TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS cases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        case_side TEXT,
        summary_format TEXT,
        created_at TEXT,
        case_folder TEXT
    )
    """)

    conn.commit()
    conn.close()


# ---------------- USER ----------------

class User(UserMixin):
    def __init__(self, id, name, email, password_hash):
        self.id = str(id)
        self.name = name
        self.email = email
        self.password_hash = password_hash


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()

    if row:
        return User(row["id"], row["name"], row["email"], row["password_hash"])
    return None


# ---------------- OLLAMA ----------------

def call_ollama(prompt):
    r = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=600
    )
    r.raise_for_status()
    return r.json()["response"]


# ---------------- OCR ----------------

def extract_text_with_ocr(pdf_path, max_pages=5):

    images = convert_from_path(str(pdf_path), first_page=1, last_page=max_pages)

    text = ""

    for img in images:
        text += pytesseract.image_to_string(img)

    return text


# ---------------- TEXT UTILS ----------------

def chunk_text(text, size=800):

    words = text.split()

    return [
        " ".join(words[i:i + size])
        for i in range(0, len(words), size)
    ]


def clean_text(text):

    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ---------------- ROUTES ----------------

@app.route("/")
def home():

    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()

        row = conn.execute(
            "SELECT * FROM users WHERE email=?",
            (email,)
        ).fetchone()

        conn.close()

        if row and check_password_hash(row["password_hash"], password):

            user = User(row["id"], row["name"], row["email"], row["password_hash"])

            login_user(user)

            return redirect(url_for("dashboard"))

        flash("Invalid login")

    return render_template("login.html")


# ---------------- REGISTER ----------------

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")

        if not name or not email or not password:
            flash("All fields required")
            return redirect(url_for("register"))

        password_hash = generate_password_hash(password)

        try:
            conn = get_db()

            conn.execute(
                "INSERT INTO users (name,email,password_hash) VALUES (?,?,?)",
                (name, email, password_hash)
            )

            conn.commit()
            conn.close()

            flash("Account created. Please login.")

            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            flash("Email already registered")
            return redirect(url_for("register"))

    return render_template("register.html")


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():

    return render_template("dashboard.html", name=current_user.name)


# ---------------- NEW CASE ----------------

@app.route("/new-case", methods=["GET", "POST"])
@login_required
def new_case():

    if request.method == "POST":

        case_side = request.form["case_side"]
        summary_format = request.form["summary_format"]

        files = request.files.getlist("documents")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        case_folder_name = f"user_{current_user.id}_{ts}"

        case_folder = UPLOADS_DIR / case_folder_name
        case_folder.mkdir(parents=True)

        saved = 0

        for f in files:

            if f.filename.endswith(".pdf"):
                f.save(case_folder / secure_filename(f.filename))
                saved += 1

        conn = get_db()

        cur = conn.execute(
            "INSERT INTO cases(user_id,case_side,summary_format,created_at,case_folder) VALUES(?,?,?,?,?)",
            (current_user.id, case_side, summary_format, datetime.now().isoformat(), case_folder_name)
        )

        case_id = cur.lastrowid

        conn.commit()
        conn.close()

        flash(f"{saved} PDFs uploaded")

        return redirect(url_for("view_case", case_id=case_id))

    return render_template("new_case.html")


# ---------------- VIEW CASE ----------------

@app.route("/case/<int:case_id>")
@login_required
def view_case(case_id):

    conn = get_db()

    row = conn.execute(
        "SELECT * FROM cases WHERE id=? AND user_id=?",
        (case_id, current_user.id)
    ).fetchone()

    conn.close()

    case_folder = UPLOADS_DIR / row["case_folder"]

    files = [p.name for p in case_folder.glob("*.pdf")]

    merged_ready = (case_folder / "merged.pdf").exists()

    return render_template(
        "case.html",
        case_id=case_id,
        files=files,
        merged_ready=merged_ready
    )


# ---------------- MERGE PDF ----------------

@app.route("/case/<int:case_id>/merge", methods=["POST"])
@login_required
def merge_case(case_id):

    conn = get_db()

    row = conn.execute(
        "SELECT * FROM cases WHERE id=? AND user_id=?",
        (case_id, current_user.id)
    ).fetchone()

    conn.close()

    case_folder = UPLOADS_DIR / row["case_folder"]

    writer = PdfWriter()

    for pdf in case_folder.glob("*.pdf"):

        reader = PdfReader(pdf)

        for page in reader.pages:
            writer.add_page(page)

    merged = case_folder / "merged.pdf"

    with open(merged, "wb") as f:
        writer.write(f)

    flash("PDFs merged")

    return redirect(url_for("view_case", case_id=case_id))


# ---------------- SUMMARIZE ----------------

@app.route("/case/<int:case_id>/summarize", methods=["POST"])
@login_required
def summarize_case(case_id):

    conn = get_db()

    row = conn.execute(
        "SELECT * FROM cases WHERE id=? AND user_id=?",
        (case_id, current_user.id)
    ).fetchone()

    conn.close()

    case_folder = UPLOADS_DIR / row["case_folder"]

    merged_path = case_folder / "merged.pdf"

    reader = PdfReader(merged_path)

    text = ""

    for page in reader.pages[:5]:

        t = page.extract_text()

        if t:
            text += t

    if len(text.split()) < 100:

        text = extract_text_with_ocr(merged_path)

    text = clean_text(text)

    chunks = chunk_text(text)

    chunk_summaries = []

    for chunk in chunks[:2]:

        prompt = f"""
You are a medical record summarization assistant.

Extract key information under these headings:

Patient Information
Chief Complaint
Mechanism of Injury
Symptoms
Diagnoses
Tests
Treatment
Disposition

Medical Record:
{chunk}
"""

        chunk_summaries.append(call_ollama(prompt))

    combined = "\n".join(chunk_summaries)

    final = call_ollama(f"""
Create a structured final medical summary.

Notes:
{combined}
""")

    return render_template(
        "summary.html",
        case_id=case_id,
        summary_text=final
    )


# ---------------- DOWNLOAD DOCX ----------------

@app.route("/case/<int:case_id>/download/<kind>")
@login_required
def download_docx(case_id, kind):

    conn = get_db()

    row = conn.execute(
        "SELECT * FROM cases WHERE id=? AND user_id=?",
        (case_id, current_user.id)
    ).fetchone()

    conn.close()

    if not row:
        flash("Case not found")
        return redirect(url_for("dashboard"))

    case_folder = UPLOADS_DIR / row["case_folder"]

    src = case_folder / "summary_text.txt"

    if not src.exists():
        flash("Summary not generated yet")
        return redirect(url_for("view_case", case_id=case_id))

    content = src.read_text()

    doc = Document()

    for line in content.split("\n"):
        doc.add_paragraph(line)

    outpath = case_folder / "summary.docx"

    doc.save(outpath)

    return send_file(outpath, as_attachment=True)


# ---------------- LOGOUT ----------------

@app.route("/logout")
@login_required
def logout():

    logout_user()

    return redirect(url_for("login"))


# ---------------- RUN ----------------

if __name__ == "__main__":

    init_db()

    app.run(debug=True)