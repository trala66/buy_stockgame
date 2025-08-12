# investeringsspil_app.py
# uses venv webdata_env

# investeringsspil_app.py
# uses venv webdata_env

import os
import time
import re
from decimal import Decimal
from datetime import timedelta, datetime, timezone
from time import sleep

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras
from psycopg2 import OperationalError
import yfinance as yf


# ──────────────────────────────────────────────────────────────────────────────
# Flask app
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "superhemmelig_lang_random")
app.permanent_session_lifetime = timedelta(minutes=45)

# Gør Jinja striks ift. manglende nøgler (god til at fange template-fejl)
from jinja2 import StrictUndefined
app.jinja_env.undefined = StrictUndefined

# ──────────────────────────────────────────────────────────────────────────────
# Database DSN
# ──────────────────────────────────────────────────────────────────────────────

def _build_db_dsn() -> str:
    """
    Foretrækker DATABASE_URL (Railway/Heroku-stil).
    Fallback til enkelte PG* env vars.
    Sikrer sslmode=require hvis ikke specificeret (Aiven kræver SSL).
    """
    url = os.getenv("DATABASE_URL")
    if url:
        # Normalisér prefix (psycopg2 kan normalt begge, men vi gør det pænt)
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        # Sørg for sslmode=require hvis mangler
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return url

    # Fallback: enkeltvariabler (nyttigt lokalt)
    user = os.getenv("PGUSER", "")
    password = os.getenv("PGPASSWORD", "")
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    database = os.getenv("PGDATABASE", "invest-game")
    sslmode = os.getenv("PGSSLMODE", "require")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}?sslmode={sslmode}"

DB_DSN = _build_db_dsn()

# Retry konfiguration for DB-forbindelse
RETRY_ATTEMPTS = int(os.getenv("DB_RETRY_ATTEMPTS", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("DB_RETRY_DELAY_SECONDS", "3"))

def get_db_connection():
    """
    Opretter forbindelse til PostgreSQL databasen med retry-mekanisme.
    Bruger RealDictCursor så Jinja kan tilgå felter som .name / ["name"].
    """
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
        except OperationalError as e:
            last_error = e
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                raise last_error

# ──────────────────────────────────────────────────────────────────────────────
# Hjælpere til aktiekurser
# ──────────────────────────────────────────────────────────────────────────────
def fetch_price_from_api(ticker: str):
    """Prøv fast_info først; fald tilbage til info['regularMarketPrice'].""" 
    try:
        t = yf.Ticker(ticker)
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            last_price = fi.get("last_price")
            if last_price is not None:
                return Decimal(str(last_price))
        price = t.info.get("regularMarketPrice")
        return Decimal(str(price)) if price is not None else None
    except Exception:
        return None

def ensure_stock_price(stock_id: int):
    """
    Returnér current_price for en aktie.
    Hvis mangler i DB, forsøg at hente og opdatere den.
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT ticker, current_price FROM stocks WHERE stock_id = %s", (stock_id,))
        row = cur.fetchone()
        if not row:
            return None
        ticker, current_price = row["ticker"], row["current_price"]
        if current_price is not None:
            return Decimal(str(current_price))
        price = fetch_price_from_api(ticker)
        if price is not None:
            cur.execute("UPDATE stocks SET current_price = %s WHERE stock_id = %s", (price, stock_id))
            conn.commit()
        return price




MIN_REFRESH_MINUTES = int(os.getenv("MIN_PRICE_REFRESH_MINUTES", "15"))

def update_stock_prices_all(source: str = "yfinance", snapshot: bool = True):
    """
    Opdatér alle aktiers current_price – men max hver MIN_REFRESH_MINUTES.
    - Globalt tidsstempel i price_refresh_control afgør om vi må kalde yfinance.
    - Tidsstempel sættes til OPDATERINGENS STARTTID (window-start), så gentagne
      klik ikke skubber intervallet.
    - Snapshot indsættes kun hvis prisen ændres.
    Returnerer (updated_count, snapshotted_count, skipped) hvor 'skipped' = True,
    hvis vi sprang opdatering over pga. rate-limit.
    """
    min_interval = timedelta(minutes=MIN_REFRESH_MINUTES)
    updated = 0
    snapshotted = 0

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Sørg for at kontrolrækken findes og lås den
            cur.execute("""
                INSERT INTO price_refresh_control (id, last_refreshed_at)
                VALUES (TRUE, NULL)
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                SELECT last_refreshed_at
                FROM price_refresh_control
                WHERE id = TRUE
                FOR UPDATE
            """)
            row = cur.fetchone()
            last = row["last_refreshed_at"] if row else None

            now = datetime.now(timezone.utc)

            # Rate-limit check
            if last is not None and (now - last) < min_interval:
                # Spring helt over – brug eksisterende priser
                print(f"[update_stock_prices_all] skipped; last={last.isoformat()} < {MIN_REFRESH_MINUTES}min")
                # Ingen commit nødvendig; vi har kun læst/låst
                return (0, 0, True)

            # Vi må opdatere – fastfrys "window start" nu,
            # og brug det som last_refreshed_at EFTER en vellykket kørsel
            window_start = now

            # Hent tickers + nuværende pris
            cur.execute("SELECT stock_id, ticker, current_price FROM stocks")
            rows = cur.fetchall()

            for r in rows:
                sid = r["stock_id"]
                ticker = r["ticker"]
                old_price = r["current_price"]

                # Prøv at hente ny pris
                new_price = fetch_price_from_api(ticker)
                if new_price is None:
                    continue

                prices_differ = (old_price is None) or (Decimal(str(old_price)) != new_price)
                if prices_differ:
                    # Hvis du IKKE bruger triggeren der sætter price_updated_at:
                    # cur.execute(
                    #   "UPDATE stocks SET current_price = %s, price_updated_at = NOW() WHERE stock_id = %s",
                    #   (new_price, sid)
                    # )
                    cur.execute(
                        "UPDATE stocks SET current_price = %s WHERE stock_id = %s",
                        (new_price, sid)
                    )
                    updated += 1

                    if snapshot:
                        # Hvis du har tabellen stock_price_snapshots:
                        # id | stock_id | price | source | captured_at
                        cur.execute(
                            """
                            INSERT INTO stock_price_snapshots (stock_id, price, source)
                            VALUES (%s, %s, %s)
                            """,
                            (sid, new_price, source)
                        )
                        snapshotted += 1

            # Sæt globalt stempel til STARTTID (ikke slut) – for at undgå "drift"
            cur.execute("""
                UPDATE price_refresh_control
                SET last_refreshed_at = %s
                WHERE id = TRUE
            """, (window_start,))

            conn.commit()

    print(f"[update_stock_prices_all] updated={updated}, snapshots={snapshotted}, window_start={window_start.isoformat()}")
    return (updated, snapshotted, False)



# ──────────────────────────────────────────────────────────────────────────────
# auth
# ──────────────────────────────────────────────────────────────────────────────


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        userid_raw = (request.form.get("userid") or "").strip()
        pin_raw    = (request.form.get("pin") or "").strip()

        # Server-side validation (never rely only on HTML)
        if not re.fullmatch(r"\d+", userid_raw):
            flash("Bruger-ID skal være et tal.", "warning")
            return render_template("login.html", userid=userid_raw)

        if not re.fullmatch(r"\d{4}", pin_raw):
            flash("PIN skal bestå af 4 cifre.", "warning")
            return render_template("login.html", userid=userid_raw)

        userid = int(userid_raw)
        pin = pin_raw

        try:
            with get_db_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, password_hash FROM users WHERE user_id = %s",
                    (userid,),
                )
                row = cur.fetchone()
        except Exception:
            app.logger.exception("Login DB error")
            flash("Teknisk fejl. Prøv igen om lidt.", "danger")
            return render_template("login.html", userid=userid_raw)

        if row and check_password_hash(row["password_hash"], pin):
            session.permanent = True
            session["user_id"] = int(row["user_id"])
            return redirect(url_for("dashboard"))

        # tiny delay to make brute-forcing slightly harder (optional)
        sleep(0.3)
        flash("Login mislykkedes. Tjek ID og PIN.", "danger")
        return render_template("login.html", userid=userid_raw)

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:30]
        pin = request.form.get("pin", "").strip()
        if not name or not pin or not pin.isdigit() or len(pin) != 4:
            flash("Udfyld navn og 4-cifret PIN korrekt.", "warning")
            return render_template("register.html")

        hashed = generate_password_hash(pin)
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (name, password_hash) VALUES (%s, %s) RETURNING user_id",
                (name, hashed),
            )
            new_id = cur.fetchone()["user_id"]
            conn.commit()
        flash(f"Bruger oprettet. Dit ID er: {new_id}", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

# ──────────────────────────────────────────────────────────────────────────────
# dashboard
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    uid = session["user_id"]
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, cash_balance FROM users WHERE user_id = %s", (uid,))
        user_row = cur.fetchone()
        cur.execute(
            """
            SELECT s.name, h.quantity, s.current_price, h.purchase_price
            FROM holdings h
            JOIN stocks s ON s.stock_id = h.stock_id
            WHERE h.user_id = %s
            ORDER BY s.name
            """,
            (uid,),
        )
        holdings = cur.fetchall()
    return render_template("dashboard.html", user=user_row, holdings=holdings)

# ──────────────────────────────────────────────────────────────────────────────
# API kald: pris til den valgte aktie (bruges af buy.html/JS)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/stock_price/<int:stock_id>")
def api_stock_price(stock_id):
    price = ensure_stock_price(stock_id)
    if price is None:
        return jsonify({"ok": False, "error": "Ingen pris tilgængelig."}), 503
    return jsonify({"ok": True, "price": float(price), "as_of": datetime.now(timezone.utc).isoformat()})
    #return jsonify({"ok": True, "price": float(price), "as_of": datetime.utcnow().isoformat() + "Z"})

# ──────────────────────────────────────────────────────────────────────────────
# Køb aktier
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/buy", methods=["GET", "POST"])
def buy():
    if "user_id" not in session:
        return redirect(url_for("login"))
    uid = session["user_id"]

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_id, name, ticker, current_price FROM stocks ORDER BY name")
        stocks = cur.fetchall()
        cur.execute("SELECT cash_balance FROM users WHERE user_id = %s", (uid,))
        balance_row = cur.fetchone()
        cash_balance = Decimal(str(balance_row["cash_balance"])) if (balance_row and balance_row["cash_balance"] is not None) else Decimal("0")

    if request.method == "POST":
        selected_stock_id_raw = request.form.get("stock_id")
        quantity_raw = request.form.get("quantity", "0").strip()

        err = None
        try:
            selected_stock_id = int(selected_stock_id_raw)
        except (TypeError, ValueError):
            err = "Vælg venligst en aktie."
            selected_stock_id = None

        try:
            quantity = int(quantity_raw)
        except (TypeError, ValueError):
            err = err or "Angiv et gyldigt antal."
            quantity = 0

        if not err and quantity <= 0:
            err = "Antal skal være større end nul."

        price = ensure_stock_price(selected_stock_id) if (not err and selected_stock_id) else None
        if not err and price is None:
            err = "Kunne ikke hente aktuel kurs. Prøv igen om lidt."

        total = (price * Decimal(quantity)).quantize(Decimal("0.01")) if (not err) else Decimal("0.00")

        if not err and total > cash_balance:
            err = (
                f"Du har ikke nok kontanter til dette køb. "
                f"Samlet pris: {total:.2f} kr. | Din saldo: {cash_balance:.2f} kr."
            )

        if err:
            flash(err, "danger")
            return render_template(
                "buy.html",
                stocks=stocks,
                cash_balance=cash_balance,
                selected_stock_id=selected_stock_id,
                selected_price=float(price) if price is not None else None,
                selected_quantity=quantity,
            )
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT cash_balance FROM users WHERE user_id = %s FOR UPDATE", (uid,))
            row = cur.fetchone()
            if not row:
                flash("Bruger ikke fundet.", "danger")
                return render_template("buy.html", stocks=stocks, cash_balance=cash_balance)

            current_balance = Decimal(str(row["cash_balance"]))
            if total > current_balance:
                flash(
                    f"Købet blev afvist: pris {total:.2f} kr. overstiger din saldo {current_balance:.2f} kr.",
                    "danger",
                )
                return render_template(
                    "buy.html",
                    stocks=stocks,
                    cash_balance=current_balance,
                    selected_stock_id=selected_stock_id,
                    selected_price=float(price),
                    selected_quantity=quantity,
                )

            cur.execute(
                """
                INSERT INTO holdings (user_id, stock_id, quantity, purchase_price)
                VALUES (%s, %s, %s, %s)
                """,
                (uid, selected_stock_id, quantity, price),
            )
            cur.execute(
                "UPDATE users SET cash_balance = cash_balance - %s WHERE user_id = %s",
                (total, uid),
            )
            conn.commit()

        flash(f"Køb gennemført: {quantity} stk. til {price:.2f} kr./stk. (i alt {total:.2f} kr.)", "success")
        return redirect(url_for("dashboard"))

    return render_template("buy.html", stocks=stocks, cash_balance=cash_balance)



# ──────────────────────────────────────────────────────────────────────────────
# Leaderboard/overview
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/overview")
def overview():
    updated, snaps, skipped = update_stock_prices_all()
    if skipped:
        flash("Kurser blev ikke hentet (opdateret for nylig – max hver 15. minut).", "info")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.user_id, u.name,
                   u.cash_balance,
                   COALESCE(SUM(h.quantity * s.current_price), 0) AS stocks_value,
                   u.cash_balance + COALESCE(SUM(h.quantity * s.current_price), 0) AS total
            FROM users u
            LEFT JOIN holdings h ON h.user_id = u.user_id
            LEFT JOIN stocks s   ON s.stock_id = h.stock_id
            GROUP BY u.user_id, u.name, u.cash_balance
            ORDER BY total DESC
            """
        )
        users = cur.fetchall()
    return render_template("overview.html", users=users)


'''
@app.route("/overview")
def overview():
    # Lad prod/hosting styre om vi auto-opdaterer priser (kan være langsomt)
    if os.getenv("AUTO_REFRESH_PRICES_ON_OVERVIEW", "false").lower() in ("1", "true", "yes"):
        update_stock_prices_all()
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.user_id, u.name,
                   u.cash_balance,
                   COALESCE(SUM(h.quantity * s.current_price), 0) AS stocks_value,
                   u.cash_balance + COALESCE(SUM(h.quantity * s.current_price), 0) AS total
            FROM users u
            LEFT JOIN holdings h ON h.user_id = u.user_id
            LEFT JOIN stocks s ON s.stock_id = h.stock_id
            GROUP BY u.user_id, u.name, u.cash_balance
            ORDER BY total DESC
            """
        )
        users = cur.fetchall()
    return render_template("overview.html", users=users)
'''
# ──────────────────────────────────────────────────────────────────────────────
# Manuel opdatering (knap)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/manual_update")
def manual_update():
    update_stock_prices_all()
    flash("Aktuelle kurser er blevet opdateret.", "info")
    return redirect(url_for("overview"))

# ──────────────────────────────────────────────────────────────────────────────
# Run globally. På Railway bruger gunicorn via Procfile.
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
