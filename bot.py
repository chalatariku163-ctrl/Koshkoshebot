import os
import time
import random
import sqlite3
import threading
import logging
import requests

from datetime import datetime
from flask import Flask, request, jsonify, send_file

# =====================================
# CONFIGURATION
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/var/data/iqub.db"
)

PAYMENT_AMOUNT = 200
TOTAL_TICKETS = 6000

FIRST_PRIZE = 500000
SECOND_PRIZE = 30000
THIRD_PRIZE = 20000

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

db_lock = threading.RLock()
polling_started = False
polling_lock = threading.Lock()


# =====================================
# DATABASE
# =====================================

def get_db():

    folder = os.path.dirname(DATABASE_PATH)

    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(
        DATABASE_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with db_lock:

        conn = get_db()

        conn.execute("PRAGMA journal_mode=WAL")

        conn.executescript("""

        CREATE TABLE IF NOT EXISTS users (

            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            created_at TEXT

        );

        CREATE TABLE IF NOT EXISTS payments (

            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            payment_method TEXT,
            transaction_reference TEXT,
            status TEXT DEFAULT 'PENDING',
            ticket_number INTEGER UNIQUE,
            created_at TEXT,
            approved_at TEXT,

            FOREIGN KEY(user_id)
            REFERENCES users(id)

        );

        CREATE INDEX IF NOT EXISTS idx_payment_status
        ON payments(status);

        CREATE INDEX IF NOT EXISTS idx_user_telegram
        ON users(telegram_id);

        """)

        conn.commit()
        conn.close()


init_db()


# =====================================
# TELEGRAM API
# =====================================

def telegram(method, data=None):

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing")
        return None

    try:

        response = requests.post(
            f"{BASE_URL}/{method}",
            json=data or {},
            timeout=40
        )

        result = response.json()

        if not result.get("ok"):
            logger.error("Telegram API: %s", result)

        return result

    except Exception as e:

        logger.exception("Telegram error: %s", e)

        return None


def send_message(chat_id, text, keyboard=None):

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return telegram("sendMessage", data)


def answer_callback(callback_id, text):

    return telegram(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text
        }
    )


# =====================================
# USER MANAGEMENT
# =====================================

def register_user(
    telegram_id,
    full_name,
    phone=""
):

    with db_lock:

        conn = get_db()

        conn.execute("""

        INSERT INTO users (
            telegram_id,
            full_name,
            phone,
            created_at
        )

        VALUES (?, ?, ?, ?)

        ON CONFLICT(telegram_id)

        DO UPDATE SET

            full_name=excluded.full_name,

            phone=CASE
                WHEN excluded.phone != ''
                THEN excluded.phone
                ELSE users.phone
            END

        """, (

            telegram_id,
            full_name,
            phone,
            datetime.now().isoformat()

        ))

        conn.commit()

        user = conn.execute("""

        SELECT *
        FROM users
        WHERE telegram_id=?

        """, (telegram_id,)).fetchone()

        conn.close()

        return dict(user)


def get_user(telegram_id):

    conn = get_db()

    user = conn.execute("""

    SELECT *
    FROM users
    WHERE telegram_id=?

    """, (telegram_id,)).fetchone()

    conn.close()

    return dict(user) if user else None


# =====================================
# TICKET GENERATION
# =====================================

def generate_ticket(conn):

    used = {

        row["ticket_number"]

        for row in conn.execute("""

        SELECT ticket_number
        FROM payments
        WHERE status='APPROVED'
        AND ticket_number IS NOT NULL

        """).fetchall()

    }

    available = [

        number

        for number in range(1, TOTAL_TICKETS + 1)

        if number not in used

    ]

    if not available:
        return None

    return random.choice(available)


# =====================================
# PAYMENT CREATION
# =====================================

def create_payment(
    telegram_id,
    full_name,
    phone,
    method,
    reference
):

    user = register_user(
        telegram_id,
        full_name,
        phone
    )

    with db_lock:

        conn = get_db()

        cursor = conn.execute("""

        INSERT INTO payments (

            user_id,
            amount,
            payment_method,
            transaction_reference,
            status,
            created_at

        )

        VALUES (?, ?, ?, ?, 'PENDING', ?)

        """, (

            user["id"],
            PAYMENT_AMOUNT,
            method,
            reference,
            datetime.now().isoformat()

        ))

        payment_id = cursor.lastrowid

        conn.commit()
        conn.close()

    return payment_id


# =====================================
# ADMIN NOTIFICATION
# =====================================

def notify_admin(payment_id):

    if not ADMIN_ID:
        logger.error("ADMIN_ID missing")
        return

    conn = get_db()

    payment = conn.execute("""

    SELECT
        payments.*,
        users.full_name,
        users.phone,
        users.telegram_id

    FROM payments

    JOIN users
    ON users.id=payments.user_id

    WHERE payments.id=?

    """, (payment_id,)).fetchone()

    conn.close()

    if not payment:
        return

    text = f"""
💰 <b>NEW EKUB PAYMENT</b>

━━━━━━━━━━━━━━━━━━

🆔 Payment ID: {payment['id']}

👤 Name: {payment['full_name']}

📱 Phone: {payment['phone']}

💵 Amount: {payment['amount']} Birr

🏦 Method: {payment['payment_method']}

🔖 Reference:
<code>{payment['transaction_reference']}</code>

⏳ Status: PENDING

━━━━━━━━━━━━━━━━━━
"""

    keyboard = {

        "inline_keyboard": [

            [

                {
                    "text": "✅ APPROVE",
                    "callback_data": f"approve_{payment_id}"
                },

                {
                    "text": "❌ REJECT",
                    "callback_data": f"reject_{payment_id}"
                }

            ]

        ]

    }

    send_message(
        ADMIN_ID,
        text,
        keyboard
    )


# =====================================
# APPROVE PAYMENT
# =====================================

def approve_payment(payment_id):

    with db_lock:

        conn = get_db()

        try:

            conn.execute("BEGIN IMMEDIATE")

            payment = conn.execute("""

            SELECT *
            FROM payments
            WHERE id=?

            """, (payment_id,)).fetchone()

            if not payment:

                conn.rollback()
                return False, "Payment not found"

            if payment["status"] != "PENDING":

                conn.rollback()
                return False, "Already processed"

            ticket = generate_ticket(conn)

            if ticket is None:

                conn.rollback()
                return False, "All tickets sold"

            conn.execute("""

            UPDATE payments

            SET
                status='APPROVED',
                ticket_number=?,
                approved_at=?

            WHERE id=?

            """, (

                ticket,
                datetime.now().isoformat(),
                payment_id

            ))

            user = conn.execute("""

            SELECT telegram_id
            FROM users
            WHERE id=?

            """, (payment["user_id"],)).fetchone()

            conn.commit()

        except Exception:

            conn.rollback()
            logger.exception("Approval error")

            return False, "Database error"

        finally:

            conn.close()

    if user:

        send_message(

            user["telegram_id"],

            f"""
🎉 <b>PAYMENT APPROVED!</b>

🏆 <b>PETROS & TOLERA</b>

<b>DIGITAL EKUB</b>

━━━━━━━━━━━━━━━━━━

💵 Payment: 200 Birr

🎟️ YOUR TICKET:

<b>#{ticket:04d}</b>

✅ Your participation is confirmed.

🥇 First Prize: 500,000 Birr

🥈 Second Prize: 30,000 Birr

🥉 Third Prize: 20,000 Birr

━━━━━━━━━━━━━━━━━━

Good Luck! 🍀
"""

        )

    return True, ticket


# =====================================
# REJECT PAYMENT
# =====================================

def reject_payment(payment_id):

    with db_lock:

        conn = get_db()

        try:

            conn.execute("BEGIN IMMEDIATE")

            payment = conn.execute("""

            SELECT *
            FROM payments
            WHERE id=?

            """, (payment_id,)).fetchone()

            if not payment:
                conn.rollback()
                return False

            if payment["status"] != "PENDING":
                conn.rollback()
                return False

            conn.execute("""

            UPDATE payments

            SET status='REJECTED'

            WHERE id=?

            """, (payment_id,))

            user = conn.execute("""

            SELECT telegram_id
            FROM users
            WHERE id=?

            """, (payment["user_id"],)).fetchone()

            conn.commit()

        except Exception:

            conn.rollback()
            logger.exception("Reject error")
            return False

        finally:

            conn.close()

    if user:

        send_message(

            user["telegram_id"],

            """
❌ <b>PAYMENT REJECTED</b>

Your payment could not be approved.

Please contact the administrator
for more information.
"""

        )

    return True


# =====================================
# MAIN KEYBOARD
# =====================================

def main_keyboard():

    buttons = []

    if WEBAPP_URL:

        buttons.append([

            {
                "text": "🌐 OPEN DIGITAL EKUB",

                "web_app": {
                    "url": WEBAPP_URL
                }

            }

        ])

    buttons.append([

        {
            "text": "👤 MY PROFILE",
            "callback_data": "profile"
        },

        {
            "text": "🎟️ MY TICKET",
            "callback_data": "ticket"
        }

    ])

    buttons.append([

        {
            "text": "💰 PAYMENT HISTORY",
            "callback_data": "history"
        }

    ])

    return {

        "keyboard": buttons,
        "resize_keyboard": True

    }


# =====================================
# TELEGRAM UPDATE HANDLER
# =====================================

def handle_update(update):

    if "message" in update:

        message = update["message"]

        chat_id = message["chat"]["id"]

        user = message.get("from", {})

        telegram_id = user.get("id")

        text = message.get("text", "")

        full_name = (

            user.get("first_name", "") +
            " " +
            user.get("last_name", "")

        ).strip()

        if not telegram_id:
            return

        if text.startswith("/start"):

            register_user(
                telegram_id,
                full_name
            )

            send_message(

                chat_id,

                f"""
🏆 <b>PETROS & TOLERA</b>

<b>DIGITAL EKUB</b>

Welcome, {full_name}!

━━━━━━━━━━━━━━━━━━

💰 Membership: 200 Birr

🎟️ Total Tickets: 6,000

🥇 First Prize: 500,000 Birr

🥈 Second Prize: 30,000 Birr

🥉 Third Prize: 20,000 Birr

━━━━━━━━━━━━━━━━━━

Press OPEN DIGITAL EKUB
to continue.
""",

                main_keyboard()

            )

        return

    if "callback_query" not in update:
        return

    callback = update["callback_query"]

    data = callback.get("data", "")

    callback_id = callback["id"]

    user_id = callback["from"]["id"]

    # APPROVE

    if data.startswith("approve_"):

        if user_id != ADMIN_ID:

            answer_callback(
                callback_id,
                "Unauthorized"
            )

            return

        payment_id = int(data.split("_")[1])

        success, result = approve_payment(payment_id)

        if success:

            answer_callback(
                callback_id,
                "Payment approved!"
            )

            send_message(

                ADMIN_ID,

                f"""
✅ PAYMENT APPROVED

Payment ID: {payment_id}

🎟️ Ticket: #{result:04d}
"""

            )

        else:

            answer_callback(
                callback_id,
                str(result)
            )

    # REJECT

    elif data.startswith("reject_"):

        if user_id != ADMIN_ID:

            answer_callback(
                callback_id,
                "Unauthorized"
            )

            return

        payment_id = int(data.split("_")[1])

        success = reject_payment(payment_id)

        answer_callback(

            callback_id,

            "Payment rejected"
            if success else "Cannot reject payment"

        )

    # PROFILE

    elif data == "profile":

        user = get_user(user_id)

        if user:

            send_message(

                user_id,

                f"""
👤 <b>MY PROFILE</b>

━━━━━━━━━━━━━━━━

Name: {user['full_name']}

Phone: {user['phone'] or 'Not added'}

Telegram ID: {user_id}

━━━━━━━━━━━━━━━━
"""

            )

        answer_callback(callback_id, "Profile")

    # TICKETS

    elif data == "ticket":

        conn = get_db()

        tickets = conn.execute("""

        SELECT ticket_number

        FROM payments

        JOIN users
        ON users.id=payments.user_id

        WHERE users.telegram_id=?

        AND payments.status='APPROVED'

        ORDER BY payments.id DESC

        """, (user_id,)).fetchall()

        conn.close()

        if tickets:

            ticket_text = "\n".join(

                f"🎟️ #{x['ticket_number']:04d}"

                for x in tickets

            )

        else:

            ticket_text = "No approved tickets yet."

        send_message(

            user_id,

            "🎟️ <b>MY TICKETS</b>\n\n" +
            ticket_text

        )

        answer_callback(callback_id, "Tickets")

    # HISTORY

    elif data == "history":

        conn = get_db()

        history = conn.execute("""

        SELECT payments.*

        FROM payments

        JOIN users
        ON users.id=payments.user_id

        WHERE users.telegram_id=?

        ORDER BY payments.id DESC

        """, (user_id,)).fetchall()

        conn.close()

        result = ""

        for item in history:

            result += (

                f"💰 {item['amount']} Birr\n"

                f"Status: {item['status']}\n"

                f"Ticket: {item['ticket_number'] or '-'}\n\n"

            )

        if not result:
            result = "No payment history."

        send_message(

            user_id,

            "📜 <b>PAYMENT HISTORY</b>\n\n" +
            result

        )

        answer_callback(callback_id, "History")


# =====================================
# TELEGRAM POLLING
# =====================================

def telegram_worker():

    if not BOT_TOKEN:

        logger.error("BOT_TOKEN missing")
        return

    # Remove old webhook before polling.

    telegram("deleteWebhook", {
        "drop_pending_updates": False
    })

    offset = None

    logger.info("Telegram polling started")

    while True:

        try:

            params = {
                "timeout": 30
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(

                f"{BASE_URL}/getUpdates",

                params=params,

                timeout=40

            )

            result = response.json()

            if not result.get("ok"):

                logger.error("Polling error: %s", result)

                time.sleep(5)
                continue

            for update in result.get("result", []):

                offset = update["update_id"] + 1

                try:

                    handle_update(update)

                except Exception:

                    logger.exception(
                        "Update handling error"
                    )

        except Exception:

            logger.exception(
                "Telegram polling error"
            )

            time.sleep(5)


def start_telegram_worker():

    global polling_started

    with polling_lock:

        if polling_started:
            return

        if not BOT_TOKEN:
            logger.error("BOT_TOKEN is not configured")
            return

        thread = threading.Thread(

            target=telegram_worker,
            daemon=True

        )

        thread.start()

        polling_started = True


# =====================================
# WEB APP
# =====================================

@app.route("/")
def home():

    if os.path.exists("index.html"):

        return send_file("index.html")

    return """
    <h1>PETROS & TOLERA DIGITAL EKUB</h1>
    <p>Application is online.</p>
    """


@app.route("/api/health")
def health():

    return jsonify({

        "status": "online",

        "project": "PETROS & TOLERA DIGITAL EKUB",

        "tickets": TOTAL_TICKETS

    })


# =====================================
# REGISTER API
# =====================================

@app.route("/api/register", methods=["POST"])
def api_register():

    data = request.get_json(silent=True) or {}

    telegram_id = data.get("telegram_id")

    full_name = data.get("full_name", "").strip()

    phone = data.get("phone", "").strip()

    if not telegram_id or not full_name:

        return jsonify({

            "success": False,
            "message": "Name and Telegram ID required"

        }), 400

    try:

        user = register_user(

            int(telegram_id),
            full_name,
            phone

        )

        return jsonify({

            "success": True,
            "user": user

        })

    except Exception:

        logger.exception("Registration error")

        return jsonify({

            "success": False,
            "message": "Registration failed"

        }), 500


# =====================================
# PAYMENT API
# =====================================

@app.route("/api/payment", methods=["POST"])
def api_payment():

    data = request.get_json(silent=True) or {}

    telegram_id = data.get("telegram_id")

    full_name = data.get("full_name", "").strip()

    phone = data.get("phone", "").strip()

    method = data.get("payment_method", "").strip()

    reference = data.get(
        "transaction_reference", ""
    ).strip()

    if not all([

        telegram_id,
        full_name,
        phone,
        method,
        reference

    ]):

        return jsonify({

            "success": False,
            "message": "All fields are required"

        }), 400

    if len(reference) > 150:

        return jsonify({

            "success": False,
            "message": "Invalid transaction reference"

        }), 400

    try:

        payment_id = create_payment(

            int(telegram_id),
            full_name,
            phone,
            method,
            reference

        )

        notify_admin(payment_id)

        return jsonify({

            "success": True,

            "payment_id": payment_id,

            "status": "PENDING",

            "amount": PAYMENT_AMOUNT,

            "message": "Payment submitted successfully"

        })

    except Exception:

        logger.exception("Payment error")

        return jsonify({

            "success": False,
            "message": "Payment submission failed"

        }), 500


# =====================================
# USER PROFILE API
# =====================================

@app.route("/api/me")
def api_me():

    telegram_id = request.args.get(
        "telegram_id",
        type=int
    )

    if not telegram_id:

        return jsonify({

            "success": False,
            "message": "Telegram ID required"

        }), 400

    user = get_user(telegram_id)

    if not user:

        return jsonify({

            "success": False,
            "message": "User not found"

        }), 404

    return jsonify({

        "success": True,
        "user": user

    })


# =====================================
# HISTORY API
# =====================================

@app.route("/api/history")
def api_history():

    telegram_id = request.args.get(
        "telegram_id",
        type=int
    )

    if not telegram_id:

        return jsonify({

            "success": False,
            "message": "Telegram ID required"

        }), 400

    conn = get_db()

    history = conn.execute("""

    SELECT payments.*

    FROM payments

    JOIN users
    ON users.id=payments.user_id

    WHERE users.telegram_id=?

    ORDER BY payments.id DESC

    """, (telegram_id,)).fetchall()

    conn.close()

    return jsonify({

        "success": True,

        "history": [dict(x) for x in history]

    })


# =====================================
# TICKET API
# =====================================

@app.route("/api/tickets")
def api_tickets():

    conn = get_db()

    rows = conn.execute("""

    SELECT ticket_number

    FROM payments

    WHERE status='APPROVED'

    AND ticket_number IS NOT NULL

    """).fetchall()

    conn.close()

    sold = sorted([

        x["ticket_number"]

        for x in rows

    ])

    return jsonify({

        "success": True,

        "total": TOTAL_TICKETS,

        "sold": sold,

        "available": TOTAL_TICKETS - len(sold)

    })


# =====================================
# ADMIN AUTHENTICATION
# =====================================

def admin_authorized():

    try:

        admin_id = int(
            request.headers.get("X-Admin-ID", "0")
        )

        return (
            ADMIN_ID != 0
            and admin_id == ADMIN_ID
        )

    except Exception:

        return False


# =====================================
# ADMIN PAYMENTS
# =====================================

@app.route("/api/admin/payments")
def admin_payments():

    if not admin_authorized():

        return jsonify({

            "success": False,
            "message": "Unauthorized"

        }), 403

    conn = get_db()

    payments = conn.execute("""

    SELECT

        payments.*,
        users.full_name,
        users.phone,
        users.telegram_id

    FROM payments

    JOIN users
    ON users.id=payments.user_id

    ORDER BY payments.id DESC

    """).fetchall()

    conn.close()

    return jsonify({

        "success": True,

        "payments": [dict(x) for x in payments]

    })


# =====================================
# ADMIN STATISTICS
# =====================================

@app.route("/api/admin/stats")
def admin_stats():

    if not admin_authorized():

        return jsonify({

            "success": False

        }), 403

    conn = get_db()

    total_users = conn.execute("""

    SELECT COUNT(*) FROM users

    """).fetchone()[0]

    approved = conn.execute("""

    SELECT COUNT(*)

    FROM payments

    WHERE status='APPROVED'

    """).fetchone()[0]

    pending = conn.execute("""

    SELECT COUNT(*)

    FROM payments

    WHERE status='PENDING'

    """).fetchone()[0]

    revenue = conn.execute("""

    SELECT COALESCE(SUM(amount), 0)

    FROM payments

    WHERE status='APPROVED'

    """).fetchone()[0]

    conn.close()

    return jsonify({

        "success": True,

        "total_users": total_users,

        "approved_payments": approved,

        "pending_payments": pending,

        "revenue": revenue,

        "sold_tickets": approved,

        "available_tickets": TOTAL_TICKETS - approved

    })


# =====================================
# ADMIN APPROVE API
# =====================================

@app.route(
    "/api/admin/approve/<int:payment_id>",
    methods=["POST"]
)
def admin_approve(payment_id):

    if not admin_authorized():

        return jsonify({

            "success": False

        }), 403

    success, result = approve_payment(payment_id)

    return jsonify({

        "success": success,

        "result": result

    })


# =====================================
# ADMIN REJECT API
# =====================================

@app.route(
    "/api/admin/reject/<int:payment_id>",
    methods=["POST"]
)
def admin_reject(payment_id):

    if not admin_authorized():

        return jsonify({

            "success": False

        }), 403

    success = reject_payment(payment_id)

    return jsonify({

        "success": success

    })


# =====================================
# START TELEGRAM WORKER
# =====================================

# Gunicorn imports this file.
# Therefore polling must start outside __main__.

start_telegram_worker()


# =====================================
# LOCAL SERVER
# =====================================

if __name__ == "__main__":

    port = int(os.getenv("PORT", "10000"))

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        use_reloader=False

    )
