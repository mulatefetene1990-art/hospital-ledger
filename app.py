#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sqlite3
import datetime
import shutil
import csv
from datetime import datetime as _dt
from io import StringIO

import requests

from flask import (
    Flask, request, render_template, render_template_string, redirect,
    flash, session, jsonify, url_for, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO
from itsdangerous import URLSafeTimedSerializer

try:
    from flask_mail import Mail, Message
    FLASK_MAIL_AVAILABLE = True
except Exception:
    Mail = None
    Message = None
    FLASK_MAIL_AVAILABLE = False


# ==========================================================
# Date conversion
# ==========================================================
def gregorian_to_ethiopian(gregorian_date):
    if isinstance(gregorian_date, str):
        if not gregorian_date or gregorian_date == "N/A":
            return "N/A"
        try:
            gregorian_date = _dt.strptime(gregorian_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return "Invalid date"

    if gregorian_date is None:
        return "N/A"

    year, month, day = gregorian_date.year, gregorian_date.month, gregorian_date.day
    if month < 9 or (month == 9 and day < 11):
        eth_year = year - 8
    else:
        eth_year = year - 7

    eth_months = [
        "Meskerem", "Tikimet", "Hidar", "Tahsas", "Ter", "Yekatit",
        "Megabit", "Miazia", "Genbot", "Sene", "Hamle", "Nehasse", "Pagume"
    ]

    gregorian_months_to_eth = {
        9: (1, 11), 10: (2, 11), 11: (3, 10), 12: (4, 10),
        1: (5, 10), 2: (6, 9), 3: (7, 11), 4: (8, 11),
        5: (9, 11), 6: (10, 11), 7: (11, 11), 8: (12, 11)
    }

    eth_month = 1
    if month in gregorian_months_to_eth:
        month_num, cutoff_day = gregorian_months_to_eth[month]
        if day >= cutoff_day:
            eth_month = month_num + 1
            if eth_month > 13:
                eth_month = 1
                eth_year += 1
        else:
            eth_month = month_num

    if eth_month > 13:
        eth_month = 1
        eth_year += 1

    eth_month_name = eth_months[eth_month - 1] if 1 <= eth_month <= 13 else "Unknown"
    return f"{eth_month_name} {day}, {eth_year}"


# 📌 ADDED: Reverse conversion engine to prevent NameError
def ethiopian_to_gregorian_string(eth_date_str):
    """
    Parses an Ethiopian date string (e.g., 'Meskerem 16, 2019')
    and returns a database-compatible Gregorian ISO string ('2026-09-26').
    """
    if not eth_date_str or eth_date_str == "N/A":
        return None
        
    try:
        parts = eth_date_str.replace(",", "").strip().split()
        if len(parts) < 3:
            return eth_date_str
            
        eth_month_name = parts[0]
        eth_day = int(parts[1])
        eth_year = int(parts[2])

        eth_months = [
            "Meskerem", "Tikimet", "Hidar", "Tahsas", "Ter", "Yekatit",
            "Megabit", "Miazia", "Genbot", "Sene", "Hamle", "Nehasse", "Pagume"
        ]
        
        if eth_month_name not in eth_months:
            return eth_date_str
            
        eth_month = eth_months.index(eth_month_name) + 1

        # Reverse alignment calculations
        if eth_month == 1:  
            gregorian_year = eth_year + 7
            gregorian_month = 9
            gregorian_day = eth_day + 10
            if gregorian_day > 30:
                gregorian_day -= 30
                gregorian_month += 1
        else:
            gregorian_year = eth_year + 7
            gregorian_month = (eth_month + 8)
            if gregorian_month > 12:
                gregorian_month -= 12
            gregorian_day = eth_day

        return f"{gregorian_year}-{gregorian_month:02d}-{gregorian_day:02d}"
    except Exception as e:
        print(f"Calendar conversion error: {e}")
        return eth_date_str


# ==========================================================
# Config
# ==========================================================
DB_PATH = os.environ.get("DB_PATH", "hospital_ledger.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-placeholder")
SECURITY_PASSWORD_SALT = os.environ.get("SECURITY_PASSWORD_SALT", "dev-password-salt")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.jinja_env.filters["to_ethiopian"] = gregorian_to_ethiopian

app.config["SECRET_KEY"] = SECRET_KEY
app.config["SECURITY_PASSWORD_SALT"] = SECURITY_PASSWORD_SALT
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

if FLASK_MAIL_AVAILABLE:
    app.config.update(
        MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT") or 587),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "True").lower() in ("1", "true", "yes"),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_DEFAULT_SENDER=os.environ.get("MAIL_DEFAULT_SENDER", "noreply@example.com"),
    )

mail = Mail(app) if FLASK_MAIL_AVAILABLE else None
serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

socketio = SocketIO(app, cors_allowed_origins="*")

STAGED_DRAFTS_REGISTRY = {}

DEPARTMENTS = {
    "CEO": {"name": "ዋና ስራ አስኪያጅ", "sender": "EXECUTIVE BOARD", "telegram_chat_id": "", "stamp_file": "stamp_ceo.png"},
    "HR": {"name": "የሰው ሃይል አስተዳደር", "sender": "HR OFFICE", "telegram_chat_id": "", "stamp_file": "stamp_hr.png"},
    "QA": {"name": "ጥ/ማ/ማ ዲፓርትመንት ", "sender": "QUALITY ASSURANCE MATRIX", "telegram_chat_id": "", "stamp_file": "stamp_qa.png"},
    "OUT": {"name": "ተመላላሽ ህክምና ዲፓርትመንት", "sender": "CLINIC DESK", "telegram_chat_id": "", "stamp_file": "stamp_out.png"},
    "ER": {"name": "ድንገተኛ ክፍል", "sender": "TRAUMA UNIT", "telegram_chat_id": "", "stamp_file": "stamp_er.png"},
    "INPAT": {"name": "አስተኝቶ ህክምና ወይም ኦ.አ.ር ዲፓርትመንት", "sender": "WARD WING", "telegram_chat_id": "", "stamp_file": "stamp_inpat.png"},
    "FIN": {"name": "ፋይናንስ ዲፓርትመንት ", "sender": "FINANCIAL OFFICE", "telegram_chat_id": "", "stamp_file": "stamp_fin.png"},
    "MED": {"name": "ሜዲካል ኦፊስ", "sender": "MEDICAL DIRECTOR", "telegram_chat_id": "", "stamp_file": "stamp_med.png"},
    "VMED": {"name": "ም/ ሜዲካል ኦፊስ", "sender": "DEPUTY DIRECTOR DESK", "telegram_chat_id": "", "stamp_file": "stamp_vmed.png"},
    "PHARM": {"name": "ፋርማሲ ዲፓርትመንት", "sender": "DISPENSARY APPARATUS", "telegram_chat_id": "", "stamp_file": "stamp_pharm.png"},
    "LAB": {"name": "ላብራቶሪ ዲፓርትመንት", "sender": "DIAGNOSTIC MATRIX", "telegram_chat_id": "", "stamp_file": "stamp_lab.png"},
    "MNCH": {"name": "MNCH ዲፓርትመንት", "sender": "MATERNAL CHILD WING", "telegram_chat_id": "", "stamp_file": "stamp_mnch.png"},
    "SEC": {"name": "ጸሃፊ ዲፓርትመንት", "sender": "SECRETARIAT DESK", "telegram_chat_id": "", "stamp_file": "stamp_sec.png"},
    "RR": {"name": "ረከርድ እና ማህደር", "sender": "RECORD DESK OFFICERS", "telegram_chat_id": "830494034", "stamp_file": "stamp_rr.png"},
    "PERS": {"name": "ፕርሶንል", "sender": "PERSONNEL OFFICE", "telegram_chat_id": "", "stamp_file": "stamp_pers.png"},
    "SEMEN": {"name": "ሰሜን ጤና ጣቢያ", "sender": "PERSONNEL OFFICE", "telegram_chat_id": "", "stamp_file": "stamp_pers.png"},
}


# ==========================================================
# DB helpers
# ==========================================================
def get_conn():
    return sqlite3.connect(DB_PATH)


def ensure_column(table_name, column_name, column_type):
    conn = get_conn()
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            conn.commit()
            print(f"Added column: {table_name}.{column_name}")
    finally:
        conn.close()


def bulletproof_database_init():
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            email TEXT,
            display_name TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_ledger (
            serial_code TEXT PRIMARY KEY,
            issue_date TEXT,
            eth_date_str TEXT,
            department TEXT,
            origin_department TEXT,
            recipient TEXT,
            recipient_personnel TEXT,
            subject TEXT,
            body TEXT,
            priority_level TEXT,
            cc_list TEXT,
            signature_name TEXT,
            approved_by_hr INTEGER DEFAULT 0,
            approved_by_ceo INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cc_signatures (
            serial_code TEXT,
            department_code TEXT,
            timestamp TEXT,
            PRIMARY KEY (serial_code, department_code)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS personnel_receipts (
            serial_code TEXT NOT NULL,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            received_at TEXT,
            PRIMARY KEY (serial_code, username)
        )
    """)

    conn.commit()
    conn.close()


bulletproof_database_init()
ensure_column("accounts", "display_name", "TEXT")
ensure_column("system_ledger", "eth_date_str", "TEXT")


def row_get(row, key, default=None):
    if row is None:
        return default
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass
    return default


def backup_db():
    ts = _dt.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = os.path.basename(DB_PATH)
    dirn = os.path.dirname(os.path.abspath(DB_PATH)) or "."
    bak = os.path.join(dirn, f"{base}.bak.{ts}")
    shutil.copy2(DB_PATH, bak)
    return bak


def generate_next_serial_code(origin_code="GEN"):
    conn = get_conn()
    cursor = conn.cursor()
    like_pattern = f"MEMO-{origin_code}-%"
    cursor.execute(
        "SELECT serial_code FROM system_ledger WHERE serial_code LIKE ? ORDER BY ROWID DESC LIMIT 1",
        (like_pattern,)
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        try:
            num_part = row[0].split("-")[-1]
            last_number = int("".join(ch for ch in num_part if ch.isdigit()))
            next_number = last_number + 1
        except Exception:
            next_number = 1
    else:
        next_number = 0

    return f"MEMO-{origin_code}-{next_number:06d}"


def send_telegram_notification(message: str, target_chat_id: str):
    if not TELEGRAM_BOT_TOKEN or "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN or not target_chat_id:
        print(f"❌ Telegram Skipped: Token or ID missing ({target_chat_id})")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=5)
        print(f"📡 Telegram status: {response.status_code}")
        print(f"📡 Telegram raw: {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Telegram failure: {e}")
        return False


def get_history_entries(user_role):
    user_role = str(user_role).upper().strip()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    base_query = """
        SELECT
            s.serial_code AS serial_code,
            s.issue_date AS issue_date,
            s.eth_date_str AS eth_date_str,
            s.department AS department,
            s.origin_department AS origin_department,
            s.recipient AS recipient,
            s.recipient_personnel AS recipient_personnel,
            s.subject AS subject,
            s.body AS body,
            s.priority_level AS priority_level,
            s.cc_list AS cc_list,
            s.signature_name AS signature_name,
            s.approved_by_hr AS approved_by_hr,
            (SELECT group_concat(cs.department_code) FROM cc_signatures cs WHERE cs.serial_code = s.serial_code) AS viewed_cc_departments,
            (SELECT COUNT(DISTINCT cs.department_code) FROM cc_signatures cs WHERE cs.serial_code = s.serial_code) AS viewed_cc_count
        FROM system_ledger s
    """

    try:
        if user_role in ("SEC", "RR", "RECORD", "RECORD_ROOM"):
            cursor.execute(base_query + " ORDER BY s.issue_date DESC, s.ROWID DESC")
        else:
            restricted_query = base_query + """
                WHERE (
                    UPPER(s.origin_department) = ?
                    OR UPPER(s.recipient) = ?
                    OR ',' || REPLACE(s.cc_list, ' ', '') || ',' LIKE ?
                )
                ORDER BY s.issue_date DESC, s.ROWID DESC
            """
            cc_match_pattern = f"%,{user_role},%"
            cursor.execute(restricted_query, (user_role, user_role, cc_match_pattern))

        entries = cursor.fetchall()
        return [dict(row) for row in entries]
    except sqlite3.Error as db_err:
        print(f"❌ Database error: {db_err}")
        return []
    finally:
        conn.close()


def get_personnel_accounts():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, username, display_name
            FROM accounts
            WHERE UPPER(role) = 'PERS'
            ORDER BY display_name COLLATE NOCASE
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ==========================================================
# Routes
# ==========================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        password2 = request.form.get("password2", "").strip()
        email = request.form.get("email", "").strip() or None
        role = request.form.get("role", "SEC").strip().upper() or "SEC"
        display_name = request.form.get("display_name", "").strip()

        if not username or not password:
            flash("Username and password required", "error")
            return redirect("/register")

        if not display_name:
            flash("Full name is required.", "error")
            return redirect("/register")

        if password != password2:
            flash("Passwords do not match", "error")
            return redirect("/register")

        conn = get_conn()
        cur = conn.cursor()

        try:
            cur.execute("PRAGMA table_info(accounts)")
            cols = [row[1] for row in cur.fetchall()]

            insert_cols = ["username", "password_hash", "role", "display_name"]
            values = [
                username,
                generate_password_hash(password),
                role,
                display_name
            ]

            if "email" in cols:
                insert_cols.append("email")
                values.append(email)

            placeholders = ",".join("?" for _ in insert_cols)
            column_list = ",".join(insert_cols)

            cur.execute(
                f"INSERT INTO accounts ({column_list}) VALUES ({placeholders})",
                values
            )
            conn.commit()

            flash("Account created successfully. Please login.", "success")
            return redirect("/login")

        except sqlite3.IntegrityError:
            flash("Username already exists.", "error")
            return redirect("/register")

        finally:
            conn.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def authentication_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("authentication_login"))

        conn = get_conn()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT
                    id,
                    username,
                    password_hash,
                    role,
                    email,
                    display_name
                FROM accounts
                WHERE username = ?
            """, (username,))

            account = cursor.fetchone()

        except sqlite3.OperationalError as error:
            print(f"Login database error: {error}")

            if "display_name" in str(error).lower():
                cursor.execute("""
                    SELECT
                        id,
                        username,
                        password_hash,
                        role,
                        email
                    FROM accounts
                    WHERE username = ?
                """, (username,))
                account = cursor.fetchone()
            else:
                conn.close()
                flash("Database error during login.", "error")
                return redirect(url_for("authentication_login"))

        finally:
            conn.close()

        if account is None:
            flash("Invalid username or password.", "error")
            return redirect(url_for("authentication_login"))

        password_hash = row_get(account, "password_hash", "")

        if not password_hash:
            flash("This account has no valid password.", "error")
            return redirect(url_for("authentication_login"))

        if not check_password_hash(password_hash, password):
            flash("Invalid username or password.", "error")
            return redirect(url_for("authentication_login"))

        # CORRECT FIX: sqlite3.Row does not support .get()
        account_username = row_get(account, "username", username)
        account_role = row_get(account, "role", "SEC")
        account_display_name = row_get(account, "display_name", account_username)

        if not account_display_name:
            account_display_name = account_username

        session.clear()
        session["username"] = account_username
        session["role"] = account_role
        session["display_name"] = account_display_name

        flash(f"Welcome back, {account_display_name}.", "success")
        return redirect("/")

    return render_template("login.html")


@app.route("/logout")
def authentication_logout():
    session.clear()
    flash("Session terminated safely.", "success")
    return redirect("/login")


@app.route("/submit-draft", methods=["POST"])
def submit_draft():
    if "username" not in session:
        return redirect(url_for("authentication_login"))

    user_role = session.get("role", "GEN")
    subject = request.form.get("subject", "").strip()
    content = request.form.get("content", "").strip()

    serial = generate_next_serial_code(user_role)
    STAGED_DRAFTS_REGISTRY[user_role] = {
        "id": serial,
        "serial_code": serial,
        "origin_department": user_role,
        "subject": subject,
        "body": content,
        "timestamp": _dt.utcnow().isoformat()
    }

    socketio.emit("pipeline_update", {
        "serial_code": serial,
        "subject": subject,
        "origin_department": user_role,
        "status": "Staged for Department Approval"
    })

    dept_info = DEPARTMENTS.get(user_role, {})
    dept_name = dept_info.get("name", user_role)
    origin_chat_id = dept_info.get("telegram_chat_id")

    if origin_chat_id:
        origin_msg = (
            f"📥 *MEMO INITIALIZED BY YOUR NODE*\n\n"
            f"📁 *Origin Node:* {dept_name}\n"
            f"🆔 *Serial Ref:* {serial}\n"
            f"📝 *Subject:* {subject}\n"
            f"📋 *Status:* Draft successfully staged in your queue dashboard view."
        )
        send_telegram_notification(origin_msg, origin_chat_id)

    rr_info = DEPARTMENTS.get("RR", {})
    rr_chat_id = rr_info.get("telegram_chat_id")
    if rr_chat_id:
        rr_msg = (
            f"📢 *ATTENTION RECORD ROOM*\n\n"
            f"An official draft has been initialized by *{dept_name}*.\n"
            f"🆔 *Serial Ref:* {serial}\n"
            f"📝 *Subject:* {subject}\n\n"
            f"👉 *Action Required:* Please approve a draft letter inside your management ledger view layout."
        )
        send_telegram_notification(rr_msg, rr_chat_id)

    flash(f"Draft successfully initialized and sent to {user_role} department for review.", "success")
    return redirect(url_for("central_dashboard_portal"))


@app.route("/stage-draft-route", methods=["POST"])
def stage_draft_route():
    if "username" not in session:
        return redirect("/login")

    issue_date = (
        request.form.get("date", "").strip()
        or request.form.get("memo_date", "").strip()
        or datetime.date.today().strftime("%Y-%m-%d")
    )
    eth_date_str = request.form.get("eth_date_str", "").strip()
    
    # Capture the dynamic highlighted day dropdown selection you added earlier
    memo_day_select = request.form.get("memo_day", "").strip()

    recipient = request.form.get("recipient", "").strip().upper()
    origin_department = request.form.get("origin_department", "").strip().upper()
    subject = request.form.get("subject", "").strip()
    
    # 🛠️ FIXED MAPPING TYPO: Changed "Fbody" to "body" to match standard HTML input name keys
    body = request.form.get("body", "").strip()
    
    priority_level = request.form.get("priority_level", "ROUTINE").strip().upper()

    selected_cc_array = [
        item.strip().upper()
        for item in request.form.getlist("cc_list")
        if item.strip()
    ]
    comma_separated_cc = ",".join(selected_cc_array)

    recipient_personnel_list = []
    recipient_personnel_usernames = []
    recipient_personnel_csv = ""

    if recipient == "PERS":
        selected_personnel_ids = request.form.getlist("personnel_account_ids")
        if not selected_personnel_ids:
            flash("Please select at least one registered personnel recipient.", "error")
            return redirect("/")

        placeholders = ",".join("?" for _ in selected_personnel_ids)
        conn = get_conn()
        conn.row_factory = sqlite3.Row

        try:
            rows = conn.execute(
                f"""
                SELECT id, username, display_name
                FROM accounts
                WHERE UPPER(role) = 'PERS'
                  AND id IN ({placeholders})
                ORDER BY display_name COLLATE NOCASE
                """,
                selected_personnel_ids
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            flash("No valid personnel recipients selected.", "error")
            return redirect("/")

        recipient_personnel_list = [row["display_name"] for row in rows]
        recipient_personnel_usernames = [row["username"] for row in rows]
        recipient_personnel_csv = ",".join(recipient_personnel_usernames)

    if not origin_department or not recipient or not subject or not body:
        flash("All fields must be completed.", "error")
        return redirect("/")

    if not eth_date_str:
        flash("Please select an Ethiopian date.", "error")
        return redirect("/")

    draft = {
        "date": issue_date,
        "eth_date_str": eth_date_str,
        "memo_day": memo_day_select, 
        "recipient": recipient,
        "recipient_personnel": recipient_personnel_csv,
        "recipient_personnel_list": recipient_personnel_list,
        "recipient_personnel_usernames": recipient_personnel_usernames,
        "subject": subject,
        "body": body,
        "priority_level": priority_level,
        "cc_list": comma_separated_cc,
        "staged_by": session["username"],
        "origin_department": origin_department,
        "status": "PENDING_APPROVAL",
        "approvals_logged": []
    }

    # Store into global active workspace memory state registries
    STAGED_DRAFTS_REGISTRY[origin_department] = draft
    STAGED_DRAFTS_REGISTRY[recipient] = draft
    
    rewrite_key = f"REWRITE_{session['username']}"
    STAGED_DRAFTS_REGISTRY.pop(rewrite_key, None)

    # 🛠️ REMOVED THE BLOCKING SERVER SOCKET EMIT CODE TO SOLVE THE PAGE HANGING ERROR PERMANENTLY

    # Resolve display mapping name safely
    full_origin_name = DEPARTMENTS.get(origin_department, {}).get("name", origin_department)
    full_recipient_name = DEPARTMENTS.get(recipient, {}).get("name", recipient)
    
    flash(f"Draft successfully sent to the {full_recipient_name} pipeline registry for verification.", "success")
    return redirect("/")



@app.route("/execute-ledger-commit", methods=["POST"])
def execute_ledger_commit():
    if "username" not in session:
        return redirect("/")

    user_role = session.get("role", "SEC")
    target_registry_key = None
    draft = None

    for registry_key, draft_data in list(STAGED_DRAFTS_REGISTRY.items()):
        if registry_key == user_role or draft_data.get("origin_department") == user_role or draft_data.get("recipient") == user_role:
            target_registry_key = registry_key
            draft = draft_data
            break

    if not draft:
        flash("Operational error: No active draft found inside your pipeline queue repository.", "error")
        return redirect("/")

    if "approvals_logged" not in draft:
        draft["approvals_logged"] = []
    if user_role not in draft["approvals_logged"]:
        draft["approvals_logged"].append(user_role)

    origin = draft.get("origin_department")
    required_approvers = [origin, "RR"]
    required_approvers = list(dict.fromkeys([x for x in required_approvers if x and x != "SEC"]))
    pending_approvers = [role for role in required_approvers if role not in draft["approvals_logged"]]

    if pending_approvers:
        next_approver = pending_approvers[0]
        STAGED_DRAFTS_REGISTRY.pop(target_registry_key, None)
        if user_role in STAGED_DRAFTS_REGISTRY:
            STAGED_DRAFTS_REGISTRY.pop(user_role, None)
        if draft.get("recipient") in STAGED_DRAFTS_REGISTRY:
            STAGED_DRAFTS_REGISTRY.pop(draft.get("recipient"), None)
        if draft.get("origin_department") in STAGED_DRAFTS_REGISTRY:
            STAGED_DRAFTS_REGISTRY.pop(draft.get("origin_department"), None)

        draft["original_creator_backup"] = draft.get("staged_by", "")
        draft["staged_by"] = f"FORWARDED_BY_{user_role}"
        STAGED_DRAFTS_REGISTRY[next_approver] = draft

        next_approver_name = DEPARTMENTS.get(next_approver, {}).get("name", next_approver)

        try:
            socketio.emit("draft_staged_alert", {
                "target_department": next_approver,
                "sender": user_role,
                "subject": f"FORWARDED FOR APPROVAL: {draft['subject']}"
            })
            socketio.emit("draft_staged_alert", {
                "target_department": "SEC",
                "sender": user_role,
                "subject": "PIPELINE_FORWARD_NOTICE"
            })
        except Exception as e:
            print(f"Socket emit error: {e}")

        if next_approver == "RR":
            flash("Draft approved by you and forwarded to Record Room for further approval.", "success")
        else:
            flash(f"Draft approved by you and forwarded to {next_approver_name} ({next_approver}) for further approval.", "success")
        return redirect("/")

    STAGED_DRAFTS_REGISTRY.pop(target_registry_key, None)

    selected_origin = draft.get("origin_department", user_role)
    serial_code = generate_next_serial_code(selected_origin)
    full_from_department_name = DEPARTMENTS.get(selected_origin, {}).get("name", "ADMINISTRATION")
    compiled_signatures = " / ".join(draft.get("approvals_logged", []))

    conn = get_conn()
    cursor = conn.cursor()
    try:
        # FIXED: Removed extra comma before closing parenthesis
        cursor.execute("""
            INSERT INTO system_ledger (
                serial_code,
                issue_date,
                eth_date_str,
                department,
                origin_department,
                recipient,
                recipient_personnel,
                subject,
                body,
                priority_level,
                cc_list,
                signature_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            serial_code,
            draft.get("date", ""),
            draft.get("eth_date_str", ""),
            full_from_department_name,
            selected_origin,
            draft.get("recipient", ""),
            draft.get("recipient_personnel", ""),
            draft.get("subject", ""),
            draft.get("body", ""),
            draft.get("priority_level", "ROUTINE"),
            draft.get("cc_list", ""),
            f"Verified & Validated by: {compiled_signatures}"
        ))

        if draft.get("recipient") == "PERS":
            personnel_usernames = draft.get("recipient_personnel_usernames", [])
            personnel_names = draft.get("recipient_personnel_list", [])

            for username, display_name in zip(personnel_usernames, personnel_names):
                cursor.execute("""
                    INSERT OR IGNORE INTO personnel_receipts (
                        serial_code,
                        username,
                        display_name,
                        received_at
                    ) VALUES (?, ?, ?, NULL)
                """, (
                    serial_code,
                    username,
                    display_name
                ))

        conn.commit()

        flash(f"Institutional Verification complete! System Memo integrated with code {serial_code}.", "success")
    except sqlite3.Error as e:
        flash(f"Database commitment fault error: {str(e)}", "error")
        print(f"Database error: {str(e)}")
    finally:
        conn.close()

    return redirect("/")


@app.route("/")
def central_dashboard_portal():
    if "username" not in session:
        return redirect("/login")

    user_role = session.get("role", "SEC")
    current_username = session.get("username")
    history_entries = get_history_entries(user_role)
    personnel_accounts = get_personnel_accounts()

    active_draft = {}
    if user_role != "SEC":
        if user_role in STAGED_DRAFTS_REGISTRY:
            active_draft = STAGED_DRAFTS_REGISTRY[user_role]
        else:
            for target_key, draft_data in STAGED_DRAFTS_REGISTRY.items():
                if draft_data.get("origin_department") == user_role and draft_data.get("status") != "REWRITE_REQUESTED":
                    active_draft = draft_data
                    break
    else:
        active_draft = STAGED_DRAFTS_REGISTRY.get(user_role, {})

    outbound_draft = None
    if user_role == "SEC":
        for target_dept, draft_data in STAGED_DRAFTS_REGISTRY.items():
            if draft_data.get("staged_by") == current_username and draft_data.get("status") != "REWRITE_REQUESTED" and not target_dept.startswith("REWRITE_"):
                outbound_draft = draft_data
                break

    rewrite_draft = {}
    if user_role == "SEC":
        rewrite_key = f"REWRITE_{current_username}"
        rewrite_draft = STAGED_DRAFTS_REGISTRY.get(rewrite_key, {})

    if rewrite_draft:
        display_draft = rewrite_draft
    elif active_draft:
        display_draft = active_draft
    elif outbound_draft:
        display_draft = outbound_draft
    else:
        display_draft = {}

    edit_mode_active = bool((active_draft and active_draft.get("status") != "REWRITE_REQUESTED") or rewrite_draft)
    badge_active = bool(active_draft or outbound_draft or rewrite_draft)

    serial_display = "SYSTEM-READY"
    if active_draft and active_draft.get("status") != "REWRITE_REQUESTED":
        serial_display = "DRAFT-PENDING-YOUR-APPROVAL"
    elif outbound_draft:
        serial_display = f"OUTBOUND-DRAFT-PENDING-AT-{outbound_draft.get('recipient')}"
    elif rewrite_draft:
        serial_display = "REWRITE-REQUESTED-MODIFICATIONS-REQUIRED"

    return render_template(
        "interface.html",
        edit_mode=edit_mode_active,
        outbound_draft=outbound_draft,
        approved_badge=badge_active,
        serial_code=serial_display,
        recipient=display_draft.get("recipient", ""),
        recipient_personnel=display_draft.get("recipient_personnel", ""),
        subject=display_draft.get("subject", ""),
        body=display_draft.get("body", ""),
        cc_list=display_draft.get("cc_list", ""),
        origin_department=display_draft.get("origin_department", ""),
        history=history_entries,
        departments=DEPARTMENTS,
        personnel_accounts=personnel_accounts,
        current_user_role=user_role,
        rewrite_mode=True if (rewrite_draft and user_role == "SEC") else False,
        rejection_notes=display_draft.get("rejection_notes", ""),
        rejected_by=display_draft.get("rejected_by", "")
    )

@app.route("/reject-for-rewrite", methods=["POST"])
def reject_for_rewrite():
    if "username" not in session:
        return redirect("/login")

    user_role = session.get("role", "SEC")
    rejection_notes = request.form.get("rejection_notes", "").strip()
    if not rejection_notes:
        flash("You must specify rewrite instructions before returning a document.", "error")
        return redirect("/")

    target_registry_key = None
    draft = None
    
    # Locate the active record relative to the record room's active session pipeline
    for registry_key, draft_data in STAGED_DRAFTS_REGISTRY.items():
        if draft_data.get("recipient") == user_role or registry_key == user_role:
            target_registry_key = registry_key
            draft = draft_data
            break

    if not draft:
        flash("Operational error: No active draft found inside your designated origin pipeline registry.", "error")
        return redirect("/")

    # 🛠️ PRESERVE DATA LOGIC: Update flags instead of popping/deleting keys completely
    draft["status"] = "REWRITE_REQUESTED"
    draft["rejection_notes"] = rejection_notes
    draft["rejected_by"] = user_role

    # Re-save inside registry so secretary can view the exact state updates without losing document data
    STAGED_DRAFTS_REGISTRY[target_registry_key] = draft
    STAGED_DRAFTS_REGISTRY[f"REWRITE_{draft.get('staged_by', 'SEC')}"] = draft

    try:
        socketio.emit("draft_staged_alert", {
            "target_department": "SEC",
            "sender": user_role,
            "subject": f"REWRITE REQ: {draft['subject']}"
        })
    except Exception as e:
        print(f"Socket emit error: {e}")

    flash("Document layout rejected and returned to the secretary workspace for modifications.", "success")
    return redirect("/")



@app.route("/view-memo/<serial_code>")
def view_memo_details(serial_code):
    if "username" not in session:
        return redirect("/login")

    user_role = str(session.get("role", "")).upper().strip()
    current_username = session.get("username", "").strip()

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT
            serial_code,
            issue_date,
            eth_date_str,
            department,
            origin_department,
            recipient,
            recipient_personnel,
            subject,
            body,
            priority_level,
            cc_list,
            signature_name,
            approved_by_hr
        FROM system_ledger
        WHERE serial_code = ?
    """, (serial_code,))

    memo = cur.fetchone()
    if not memo:
        conn.close()
        flash("Memo not found", "error")
        return redirect("/")

    memo_dict = dict(memo)
    cc_string = memo_dict.get("cc_list", "") or ""
    cc_members_list = [c.strip().upper() for c in cc_string.split(",") if c.strip()]

    if user_role in cc_members_list:
        current_ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        try:
            cur.execute("""
                INSERT OR IGNORE INTO cc_signatures (serial_code, department_code, timestamp)
                VALUES (?, ?, ?)
            """, (serial_code, user_role, current_ts))
            conn.commit()
        except Exception as write_err:
            print(f"CC insert bypassed: {write_err}")

    personnel_receipts = []
    if memo_dict.get("recipient") == "PERS":
        cur.execute("""
            SELECT username, display_name, received_at
            FROM personnel_receipts
            WHERE serial_code = ?
            ORDER BY rowid
        """, (serial_code,))
        personnel_rows = cur.fetchall()

        for row in personnel_rows:
            personnel_receipts.append({
                "username": row["username"],
                "display_name": row["display_name"],
                "received_at": row["received_at"],
                "status": "RECEIVED" if row["received_at"] else "PENDING"
            })

        if current_username:
            cur.execute("""
                SELECT username, display_name, received_at
                FROM personnel_receipts
                WHERE serial_code = ?
                  AND username = ?
            """, (serial_code, current_username))
            own_row = cur.fetchone()

            if own_row and not own_row["received_at"]:
                current_ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute("""
                    UPDATE personnel_receipts
                    SET received_at = ?
                    WHERE serial_code = ?
                      AND username = ?
                """, (current_ts, serial_code, current_username))
                conn.commit()

                cur.execute("""
                    SELECT username, display_name, received_at
                    FROM personnel_receipts
                    WHERE serial_code = ?
                    ORDER BY rowid
                """, (serial_code,))
                personnel_receipts = []
                for item in cur.fetchall():
                    personnel_receipts.append({
                        "username": item["username"],
                        "display_name": item["display_name"],
                        "received_at": item["received_at"],
                        "status": "RECEIVED" if item["received_at"] else "PENDING"
                    })

    cur.execute("SELECT COUNT(DISTINCT department_code) FROM cc_signatures WHERE serial_code = ?", (serial_code,))
    read_count_row = cur.fetchone()
    read_count = int(read_count_row[0]) if read_count_row and read_count_row[0] is not None else 0

    total_required_cc = len(cc_members_list)
    all_cc_have_read = bool(total_required_cc > 0 and read_count >= total_required_cc)

    if all_cc_have_read:
        cur.execute("""
            UPDATE system_ledger
            SET approved_by_hr = 1
            WHERE serial_code = ? AND approved_by_hr = 0
        """, (serial_code,))
        conn.commit()
        memo_dict["approved_by_hr"] = 1

    cur.execute("SELECT group_concat(department_code) FROM cc_signatures WHERE serial_code = ?", (serial_code,))
    history_row = cur.fetchone()
    memo_dict["viewed_cc_departments"] = history_row[0] if (history_row and history_row[0]) else ""

    conn.close()

    try:
        socketio.emit("cc_status_updated", {
            "serial_code": serial_code,
            "status": "Approved" if memo_dict.get("approved_by_hr") == 1 else "Pending"
        })
    except Exception:
        pass

    return render_template(
        "view.html",
        serial_code=serial_code,
        memo=memo_dict,
        departments=DEPARTMENTS,
        personnel_receipts=personnel_receipts,
        is_opened_and_read=True,
        all_cc_have_read=all_cc_have_read
    )


@app.route("/clear-staged-draft", methods=["POST"])
def clear_staged_draft():
    if "username" in session:
        user_role = session.get("role")
        if user_role == "SEC":
            current_username = session.get("username")
            to_remove = [k for k, v in STAGED_DRAFTS_REGISTRY.items() if v.get("staged_by") == current_username]
            for k in to_remove:
                STAGED_DRAFTS_REGISTRY.pop(k, None)
        else:
            STAGED_DRAFTS_REGISTRY.pop(user_role, None)
    return jsonify({"status": "success"})


@app.route("/download-rr-memos", methods=["POST"])
def download_rr_memos():
    if "username" not in session:
        return redirect("/login")

    user_role = session.get("role", "").upper()
    if user_role not in ["RR", "RECORD", "RECORD_ROOM"]:
        flash("Access denied: Record Room role required", "error")
        return redirect("/")

    from_date_raw = request.form.get("from_date", "").strip()
    to_date_raw = request.form.get("to_date", "").strip()
    export_format = request.form.get("format", "csv")

    if not from_date_raw or not to_date_raw:
        flash("Please select both From and To dates", "error")
        return redirect("/")

    # 🛠️ FIXED INDENTATION: Aligned safely inside the routing block
    from_date = ethiopian_to_gregorian_string(from_date_raw)
    to_date = ethiopian_to_gregorian_string(to_date_raw)

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Query matching standard date column formatting parameters
    cursor.execute("""
        SELECT serial_code, issue_date, department, origin_department,
               recipient, recipient_personnel, subject, priority_level, cc_list
        FROM system_ledger
        WHERE issue_date BETWEEN ? AND ?
        ORDER BY issue_date DESC
    """, (from_date, to_date))
    memos = cursor.fetchall()
    conn.close()

    if not memos:
        flash(f"No memos found between Ethiopian dates {from_date_raw} and {to_date_raw}", "info")
        return redirect("/")

    if export_format == "csv":
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Serial Code", "Date", "From Department", "To Department",
            "Subject", "Priority", "Personnel", "CC List"
        ])

        for memo in memos:
            writer.writerow([
                memo["serial_code"],
                memo["issue_date"],
                memo["origin_department"],
                memo["recipient"],
                memo["subject"],
                memo["priority_level"],
                memo["recipient_personnel"] or "",
                memo["cc_list"] or ""
            ])

        output.seek(0)
        return send_file(
            StringIO(output.getvalue()),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"memos_report.csv"
        )

    elif export_format == "pdf":
        html_content = render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Memos Report</title>
                <style>
                    body { font-family: Arial; margin: 20px; }
                    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
                    th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
                    th { background-color: #003366; color: white; }
                    h1 { color: #003366; }
                </style>
            </head>
            <body>
                <h1>📋 Ethiopian Calendar Memos Report</h1>
                <p><strong>Period (Ethiopian):</strong> {{ from_date }} to {{ to_date }}</p>
                <p><strong>Total Memos:</strong> {{ memos|length }}</p>

                <table>
                    <thead>
                        <tr>
                            <th>Serial Code</th>
                            <th>Date</th>
                            <th>From Dept</th>
                            <th>To Dept</th>
                            <th>Subject</th>
                            <th>Priority</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for memo in memos %}
                        <tr>
                            <td><code>{{ memo.serial_code }}</code></td>
                            <td>{{ memo.issue_date }}</td>
                            <td>{{ memo.origin_department }}</td>
                            <td>{{ memo.recipient }}</td>
                            <td>{{ memo.subject }}</td>
                            <td>{{ memo.priority_level }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </body>
            </html>
        """, from_date=from_date_raw, to_date=to_date_raw, memos=memos)

        return render_template_string(html_content)

    flash("Invalid export format", "error")
    return redirect("/")



@app.route("/search-memo", methods=["POST"])
def search_memo():
    if "username" not in session:
        return redirect("/login")

    serial_code = request.form.get("serial_code", "").strip().upper()
    if not serial_code:
        flash("Please enter a serial code", "error")
        return redirect("/")

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            serial_code, issue_date, eth_date_str, department,
            origin_department, recipient, recipient_personnel, subject,
            body, priority_level, cc_list, signature_name, approved_by_hr
        FROM system_ledger
        WHERE UPPER(serial_code) = ?
    """, (serial_code,))
    memo = cursor.fetchone()
    conn.close()

    if not memo:
        flash(f"Memo with serial code '{serial_code}' not found", "error")
        return redirect("/")

    return redirect(f"/view-memo/{serial_code}")


# ==========================================================
# Routes: Password Reset Workflow
# ==========================================================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        step = request.form.get("step")

        # --- STEP 1: Verify Username Exists ---
        if step == "verify_user":
            username = request.form.get("username", "").strip()
            
            if not username:
                flash("እባክዎ የመለያ ስም ያስገቡ (Please enter a username)", "danger")
                return render_template("forgot_password.html", user_verified=False)

            conn = get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM accounts WHERE username = ?", (username,))
            user = cursor.fetchone()
            conn.close()

            if user:
                # Username matches database! Re-render same page but display the password inputs
                return render_template("forgot_password.html", user_verified=True, verified_username=username)
            else:
                flash("የመለያ ስም አልተገኘም (Username credential not found)", "danger")
                return render_template("forgot_password.html", user_verified=False)

        # --- STEP 2: Update the Password ---
        elif step == "update_password":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not password or password != confirm_password:
                flash("የይለፍ ቃላቱ አይዛመዱም (Passwords do not match)", "danger")
                return render_template("forgot_password.html", user_verified=True, verified_username=username)

            if len(password) < 8:
                flash("የይለፍ ቃል ቢያንስ 8 ቁምፊዎች መሆን አለበት (Password must be at least 8 characters)", "danger")
                return render_template("forgot_password.html", user_verified=True, verified_username=username)

            # Securely hash new password using Werkzeug configuration
            hashed_pw = generate_password_hash(password)
            
            conn = get_conn()
            cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET password_hash = ? WHERE username = ?", (hashed_pw, username))
            conn.commit()
            conn.close()

            flash("የይለፍ ቃልዎ በተሳካ ሁኔታ ተቀይሯል! አሁን መግባት ይችላሉ። (Password updated successfully!)", "success")
            return redirect(url_for("authentication_login"))

    # Initial GET Request loading step 1
    return render_template("forgot_password.html", user_verified=False)




@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password_with_token(token):
    try:
        # Validate signature authenticity and ensure it is under the 1-hour expiration matrix
        username = serializer.loads(
            token,
            salt=app.config["SECURITY_PASSWORD_SALT"],
            max_age=3600
        )
    except Exception:
        flash("የማገገሚያ ሊንኩ ጊዜው አልፏል ወይም ልክ ያልሆነ ነው (The reset token is invalid or expired)", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not password or password != confirm_password:
            flash("የይለፍ ቃላቱ አይዛመዱም (Passwords do not match)", "danger")
            return render_template("reset_password.html")

        if len(password) < 8:
            flash("የይለፍ ቃል ቢያንስ 8 ቁምፊዎች መሆን አለበት (Password must be at least 8 characters)", "danger")
            return render_template("reset_password.html")

        # Hash new credentials matching system database encryption rules
        hashed_pw = generate_password_hash(password)
        
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("UPDATE accounts SET password_hash = ? WHERE username = ?", (hashed_pw, username))
        conn.commit()
        conn.close()

        flash("የይለፍ ቃልዎ በተሳካ ሁኔታ ተቀይሯል! አሁን መግባት ይችላሉ። (Password updated successfully!)", "success")
        return redirect(url_for("authentication_login"))

    # Serves the reset_password.html template you saved earlier
    return render_template("reset_password.html")



@app.route("/forgot-username", methods=["GET", "POST"])
def forgot_username():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if not email:
            flash("Please enter your email address.", "error")
            return redirect("/forgot-username")

        conn = get_conn()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT username, email FROM accounts WHERE email = ?", (email,))
        row = cur.fetchone()
        conn.close()

        if row:
            username = row_get(row, "username")
            stored_email = row_get(row, "email")
            if stored_email and mail:
                try:
                    msg = Message("Your account username", recipients=[stored_email])
                    msg.body = f"Hello,\n\nThe username associated with this email is: {username}\n\nIf you did not request this, ignore this message."
                    mail.send(msg)
                except Exception:
                    print("Username recovery: username for", email, "is", username)
            else:
                print("Username recovery: username for", email, "is", username)

        flash("If an account exists for that email, guidance has been sent.", "success")
        return redirect("/login")

    return render_template("forgot_username.html")


@socketio.on("connect")
def handle_socket_connection():
    print("Socket connected successfully.")


if __name__ == "__main__":
    print("Using DB_PATH =", os.path.abspath(DB_PATH))
    socketio.run(app, debug=True)