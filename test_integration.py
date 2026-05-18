"""Test d'intégration : démarre uvicorn en background, exerce TOUS les
endpoints touchés ou ajoutés, et vérifie les codes HTTP + payloads.

Usage :  .venv/bin/python test_integration.py

Quitte avec exit code 0 si tout passe, 1 sinon. Affiche un rapport coloré.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

# ─── Setup ───────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

JWT_SECRET = secrets.token_urlsafe(48)
S2S_KEY = secrets.token_urlsafe(32)
ADMIN_EMAIL = "admin.test@radar-mr.com"

env = {
    **os.environ,
    "JWT_SECRET": JWT_SECRET,
    "S2S_API_KEYS": f"debloquemoi:{S2S_KEY}",
    "ADMIN_EMAIL": ADMIN_EMAIL,
    "DATABASE_URL": f"sqlite:///{DATA}/test_integration.db",
    "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
    "AUTH_REQUIRED": "true",
    "PATH": os.environ["PATH"],
}

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"

results: list[tuple[str, bool, str]] = []


def log_test(name: str, ok: bool, detail: str = "") -> None:
    badge = f"{GREEN}✓ PASS{RESET}" if ok else f"{RED}✗ FAIL{RESET}"
    print(f"  {badge}  {name}" + (f"  {YELLOW}{detail}{RESET}" if detail else ""))
    results.append((name, ok, detail))


def section(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}")


def fatal(msg: str) -> None:
    print(f"{RED}FATAL: {msg}{RESET}")
    sys.exit(1)


# ─── Démarrage serveur ──────────────────────────────────────────────────────
def start_server() -> subprocess.Popen:
    # Nettoie l'ancienne DB de test
    db_path = DATA / "test_integration.db"
    db_path.unlink(missing_ok=True)
    topups = DATA / "topups"
    if topups.is_dir():
        shutil.rmtree(topups)

    venv_python = HERE / ".venv" / "bin" / "uvicorn"
    proc = subprocess.Popen(
        [str(venv_python), "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        env=env,
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Attend que le serveur soit prêt
    for _ in range(40):
        try:
            r = httpx.get(f"{BASE}/health", timeout=1.0)
            if r.status_code == 200:
                return proc
        except httpx.RequestError:
            pass
        time.sleep(0.25)
    err = proc.stderr.read().decode() if proc.stderr else ""
    proc.kill()
    fatal(f"Le serveur n'a pas démarré.\n{err[-2000:]}")
    return proc  # unreachable


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ─── Helpers ─────────────────────────────────────────────────────────────────
def post(path: str, *, json_body=None, headers=None, files=None) -> httpx.Response:
    return httpx.post(f"{BASE}{path}", json=json_body, headers=headers, files=files, timeout=10.0)


def get(path: str, *, headers=None) -> httpx.Response:
    return httpx.get(f"{BASE}{path}", headers=headers, timeout=10.0)


def auth_header(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def s2s_header() -> dict:
    return {"X-Api-Key": S2S_KEY}


def expect(r: httpx.Response, status: int, name: str, contains_key: str | None = None) -> bool:
    if r.status_code != status:
        log_test(name, False, f"HTTP {r.status_code} (attendu {status}) — {r.text[:200]}")
        return False
    if contains_key:
        try:
            j = r.json()
            if contains_key not in j:
                log_test(name, False, f"clé '{contains_key}' manquante — {j}")
                return False
        except Exception:
            log_test(name, False, f"JSON invalide — {r.text[:200]}")
            return False
    log_test(name, True)
    return True


# ─── Promotion admin (set is_admin=1 en DB) ──────────────────────────────────
def promote_admin(email: str) -> None:
    """Bypass : on met is_admin=1 directement via le module DB de l'app."""
    sys.path.insert(0, str(HERE))
    # Charge l'env DB
    os.environ["DATABASE_URL"] = env["DATABASE_URL"]
    from database import SessionLocal  # type: ignore
    from models import User  # type: ignore

    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        if u:
            u.is_admin = True
            db.commit()


# ─── Tests ──────────────────────────────────────────────────────────────────
def test_health():
    section("Health & routes publiques")
    r = get("/health")
    expect(r, 200, "GET /health → 200", "status")


def test_register_login_me(state: dict) -> None:
    section("Auth : register / login / me")

    # Register user A (admin futur)
    r = post(
        "/api/auth/register",
        json_body={
            "email": ADMIN_EMAIL,
            "password": "motdepasseTest123",
            "nni": "1234567890",
            "whatsapp": "+22245123456",
        },
    )
    if expect(r, 200, "POST /api/auth/register (user A)", "access_token"):
        state["tokenA"] = r.json()["access_token"]

    # Register user B (avec referral code de A → on l'aura plus tard)
    r = post(
        "/api/auth/register",
        json_body={
            "email": "userB@radar-mr.com",
            "password": "motdepasseTest123",
            "nni": "9876543210",
            "whatsapp": "+22245987654",
        },
    )
    if expect(r, 200, "POST /api/auth/register (user B)", "access_token"):
        state["tokenB"] = r.json()["access_token"]

    # Email déjà pris (l'API renvoie 400 avec un message clair)
    r = post(
        "/api/auth/register",
        json_body={
            "email": ADMIN_EMAIL,
            "password": "motdepasseTest123",
            "nni": "5555555555",
            "whatsapp": "+22245555555",
        },
    )
    expect(r, 400, "POST /api/auth/register (email dupliqué) → 400")

    # Login user A
    r = post(
        "/api/auth/login",
        json_body={"email": ADMIN_EMAIL, "password": "motdepasseTest123"},
    )
    expect(r, 200, "POST /api/auth/login (user A) → 200", "access_token")

    # Login mauvais mot de passe
    r = post(
        "/api/auth/login",
        json_body={"email": ADMIN_EMAIL, "password": "MAUVAIS"},
    )
    expect(r, 401, "POST /api/auth/login (mauvais MDP) → 401")

    # GET /me — réponse imbriquée : {authenticated, user: {email, ...}}
    r = get("/api/auth/me", headers=auth_header(state["tokenA"]))
    if r.status_code == 200 and r.json().get("user", {}).get("email"):
        log_test("GET /api/auth/me (JWT valide)", True)
    else:
        log_test("GET /api/auth/me (JWT valide)", False, f"HTTP {r.status_code} — {r.text[:200]}")

    # GET /me sans JWT
    r = get("/api/auth/me")
    expect(r, 401, "GET /api/auth/me (sans JWT) → 401")


def test_credits_me(state: dict) -> None:
    section("Credits : /credits/me")

    r = get("/api/credits/me", headers=auth_header(state["tokenA"]))
    if expect(r, 200, "GET /api/credits/me (user A)", "credit_balance"):
        state["userA_balance_initial"] = r.json()["credit_balance"]

    r = get("/api/credits/me")
    expect(r, 401, "GET /api/credits/me (sans JWT) → 401")

    r = get("/api/credits/pricing-info")
    expect(r, 200, "GET /api/credits/pricing-info", "mru_per_usd")


def test_referrals(state: dict) -> None:
    section("Referrals : /referrals/me")

    r = get("/api/referrals/me", headers=auth_header(state["tokenA"]))
    if expect(r, 200, "GET /api/referrals/me (user A)", "referral_code"):
        state["referral_code_A"] = r.json().get("referral_code")


def test_topup_request(state: dict) -> None:
    section("Top-ups : création + lecture")

    # POST topup avec un faux fichier image
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    files = {"file": ("preuve.png", io.BytesIO(fake_png), "image/png")}
    r = httpx.post(
        f"{BASE}/api/credits/topup-requests",
        files=files,
        headers=auth_header(state["tokenA"]),
        timeout=10.0,
    )
    if expect(r, 200, "POST /api/credits/topup-requests", "id"):
        state["topup_id"] = r.json()["id"]

    # GET mine
    r = get("/api/credits/topup-requests/mine", headers=auth_header(state["tokenA"]))
    expect(r, 200, "GET /api/credits/topup-requests/mine", "requests")


def test_admin_flow(state: dict) -> None:
    section("Admin : promotion + approve topup + grant manuel")

    # Promotion admin (bypass)
    promote_admin(ADMIN_EMAIL)
    log_test("Promotion is_admin=true via DB direct", True)

    # Re-login pour avoir un JWT à jour (au cas où is_admin soit dans le payload)
    r = post(
        "/api/auth/login",
        json_body={"email": ADMIN_EMAIL, "password": "motdepasseTest123"},
    )
    if r.status_code == 200:
        state["tokenA"] = r.json()["access_token"]

    # List topups pending
    r = get("/api/admin/credit-topups?status=pending", headers=auth_header(state["tokenA"]))
    expect(r, 200, "GET /api/admin/credit-topups?status=pending", "requests")

    # Get proof
    if "topup_id" in state:
        r = get(
            f"/api/admin/credit-topups/{state['topup_id']}/proof",
            headers=auth_header(state["tokenA"]),
        )
        ok = r.status_code == 200 and len(r.content) > 0
        log_test("GET /api/admin/credit-topups/{id}/proof", ok, f"size={len(r.content)}")

        # Approve topup — l'API attend "mru_credit" (en MRU) ou "supplier_cost_usd" ou "credit_amount"
        r = post(
            f"/api/admin/credit-topups/{state['topup_id']}/approve",
            json_body={"mru_credit": 100, "admin_note": "Test"},
            headers=auth_header(state["tokenA"]),
        )
        expect(r, 200, "POST /api/admin/credit-topups/{id}/approve")

    # User B id ?
    r = get(
        "/api/admin/users/search?email=userB@radar-mr.com",
        headers=auth_header(state["tokenA"]),
    )
    if expect(r, 200, "GET /api/admin/users/search", "users"):
        users = r.json().get("users", [])
        if users:
            state["userB_id"] = users[0]["id"]

    # Grant wallet à user B — l'API attend "mru_credit" (ou supplier_cost_usd ou credit_amount)
    if "userB_id" in state:
        r = post(
            f"/api/admin/users/{state['userB_id']}/grant-wallet",
            json_body={"mru_credit": 50, "admin_note": "Bonus test"},
            headers=auth_header(state["tokenA"]),
        )
        expect(r, 200, "POST /api/admin/users/{id}/grant-wallet")


def test_s2s(state: dict) -> None:
    section("S2S : /s2s/wallet/{me,debit,credit}")

    # /me sans clé
    r = get("/s2s/wallet/me?user_id=1")
    expect(r, 401, "GET /s2s/wallet/me sans X-Api-Key → 401")

    # /me avec mauvaise clé
    r = get("/s2s/wallet/me?user_id=1", headers={"X-Api-Key": "wrongkey16chars!"})
    expect(r, 403, "GET /s2s/wallet/me avec mauvaise clé → 403")

    # /me avec bonne clé, user inexistant
    r = get("/s2s/wallet/me?user_id=99999", headers=s2s_header())
    expect(r, 404, "GET /s2s/wallet/me user inexistant → 404")

    # /me avec bonne clé, user A (id=1)
    r = get("/s2s/wallet/me?user_id=1", headers=s2s_header())
    if expect(r, 200, "GET /s2s/wallet/me user existant", "balance_units"):
        state["userA_balance_s2s"] = r.json()["balance_units"]

    # Débit 1000 unités
    r = post(
        "/s2s/wallet/debit",
        json_body={
            "user_id": 1,
            "amount_units": 1000,
            "external_ref": "test-1",
            "note": "Test debit",
        },
        headers=s2s_header(),
    )
    if expect(r, 200, "POST /s2s/wallet/debit", "balance_units"):
        new_balance = r.json()["balance_units"]
        expected = state["userA_balance_s2s"] - 1000
        ok = new_balance == expected
        log_test(
            f"Débit cohérent ({state['userA_balance_s2s']} − 1000 = {new_balance})",
            ok,
            "" if ok else f"attendu {expected}",
        )

    # Crédit 500
    r = post(
        "/s2s/wallet/credit",
        json_body={"user_id": 1, "amount_units": 500, "note": "Test credit"},
        headers=s2s_header(),
    )
    expect(r, 200, "POST /s2s/wallet/credit", "balance_units")

    # Débit montant invalide
    r = post(
        "/s2s/wallet/debit",
        json_body={"user_id": 1, "amount_units": -10},
        headers=s2s_header(),
    )
    expect(r, 422, "POST /s2s/wallet/debit amount<=0 → 422")


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    print(f"{BOLD}Lancement du serveur de test sur {BASE}...{RESET}")
    proc = start_server()
    state: dict = {}
    try:
        test_health()
        test_register_login_me(state)
        test_credits_me(state)
        test_referrals(state)
        test_topup_request(state)
        test_admin_flow(state)
        test_s2s(state)
    finally:
        stop_server(proc)

    # ─── Rapport ─────────────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"\n{BOLD}━━━ Rapport ━━━{RESET}")
    print(f"{GREEN}{passed} OK{RESET} · {RED if failed else GREEN}{failed} KO{RESET} · {total} total")
    if failed:
        print(f"\n{RED}Échecs :{RESET}")
        for name, ok, detail in results:
            if not ok:
                print(f"  • {name}  {YELLOW}{detail}{RESET}")
        sys.exit(1)
    print(f"{GREEN}🎉 Tous les endpoints exercés répondent correctement.{RESET}")


if __name__ == "__main__":
    main()
