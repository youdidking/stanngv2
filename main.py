#!/usr/bin/env python3
"""
StanNG — a single-service VLESS-over-WebSocket panel, wizarding-academy themed.
Version 1.5.3 — plain-text subscription with Info Configs + TLS, compatible with v2rayNG.
Fixed: subscription links are always generated with https:// scheme.
"""
import asyncio
import base64
import io
import json
import os
import re
import secrets
import time
import uuid as uuid_lib
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

import httpx
import psutil
import qrcode
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from storage import store, hash_password, verify_password
import xray_manager
from colo_map import describe_colo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.5.4"
PANEL_NAME = "StanNG"
TELEGRAM_CONTACT = "https://t.me/rvivl"
OTA_REPO = "youdidking/stanngv2"
OTA_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": f"StanNG-Panel/{APP_VERSION}",
}
SESSION_COOKIE = "stanng_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
LOGIN_MAX_ATTEMPTS = 6
LOGIN_LOCK_SECONDS = 5 * 60

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

runtime = {
    "active": {},
    "pending_traffic": {},
    "lock": asyncio.Lock(),
}


DOH_PRIMARY = "https://1.1.1.1/dns-query"
DOH_SECONDARY = "https://8.8.8.8/dns-query"
doh_http_client = httpx.AsyncClient(timeout=6.0, follow_redirects=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    flush_task = asyncio.create_task(_periodic_flush())
    keepalive_task = asyncio.create_task(_keep_alive_loop())
    housekeep_task = asyncio.create_task(_housekeeping_loop())
    
    # Initialize Xray config and start it
    db = await store.get()
    xray_manager.generate_xray_config(db.get("inbounds", []))
    xray_manager.restart_xray()
    
    yield
    for t in (flush_task, keepalive_task, housekeep_task):
        t.cancel()
    await doh_http_client.aclose()


app = FastAPI(title="StanNG", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# ------------------------------------------------------------------ helpers
def get_serializer(db) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(db["secret_key"], salt="stanng-session")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def current_username(request: Request) -> Optional[str]:
    db = await store.get()
    if not db.get("admin"):
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    s = get_serializer(db)
    try:
        data = s.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    admin = db["admin"]
    if data.get("u") != admin.get("username"):
        return None
    if data.get("v") != admin.get("password_hash", "")[:12]:
        return None
    return admin["username"]


async def require_auth(request: Request) -> str:
    user = await current_username(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def set_session_cookie(response: Response, request: Request, db, username: str):
    s = get_serializer(db)
    token = s.dumps({"u": username, "v": db["admin"]["password_hash"][:12]})
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, httponly=True,
        samesite="lax", secure=(request.url.scheme == "https"),
        path="/",
    )


def gen_uid() -> str:
    return secrets.token_hex(8)


def gen_uuid() -> str:
    return str(uuid_lib.uuid4())


def public_host(request: Request, db) -> str:
    override = (db.get("settings") or {}).get("public_domain") or ""
    if override:
        return override.strip().split(":")[0]
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.hostname or ""
    if host.startswith("["):
        return host.split("]")[0].lstrip("[")
    return host.split(":")[0]


def get_scheme(request: Request = None) -> str:
    """Always return https scheme for subscription and public endpoints."""
    return "https"


def inbound_by_uid(db, uid: str):
    for ib in db["inbounds"]:
        if ib["uid"] == uid:
            return ib
    return None


def inbound_status(ib) -> dict:
    now = time.time()
    quota_bytes = (ib.get("quota_gb") or 0) * 1024 ** 3
    used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0)
    quota_exceeded = quota_bytes > 0 and used >= quota_bytes
    expired = False
    expire_at = ib.get("expire_at")
    if expire_at:
        expired = now >= expire_at
    live_enabled = ib.get("enabled", True) and not quota_exceeded and not expired
    active_count = len(runtime["active"].get(ib["uid"], {}))
    req_exceeded = (ib.get("max_requests") or 0) > 0 and (ib.get("request_count") or 0) >= ib["max_requests"]
    return {
        "quota_bytes": quota_bytes,
        "used": used,
        "quota_exceeded": quota_exceeded,
        "expired": expired,
        "live_enabled": live_enabled and not req_exceeded,
        "active_connections": active_count,
        "request_exceeded": req_exceeded,
        "days_left": max(0, int((expire_at - now) // 86400)) if expire_at else None,
    }


# ------------------------------------------------------------------ background tasks
async def _periodic_flush():
    while True:
        try:
            await asyncio.sleep(5)
            snapshot = await xray_manager.get_xray_stats()
            # حتی اگر snapshot خالی باشد، باکت ساعتی را به‌روز می‌کنیم

            def _apply(db, snap=snapshot):
                total_up = total_down = 0
                if snap:
                    for uid, delta in snap.items():
                        ib = inbound_by_uid(db, uid)
                        if ib:
                            ib["used_up"] = ib.get("used_up", 0) + delta.get("up", 0)
                            ib["used_down"] = ib.get("used_down", 0) + delta.get("down", 0)
                            total_up += delta.get("up", 0)
                            total_down += delta.get("down", 0)
                    db["stats"]["total_up"] = db["stats"].get("total_up", 0) + total_up
                    db["stats"]["total_down"] = db["stats"].get("total_down", 0) + total_down

                # همیشه باکت ساعتی را به‌روز کن (حتی با مقدار صفر)
                hourly = db["stats"].setdefault("hourly", [])
                bucket = int(time.time() // 3600) * 3600
                if hourly and hourly[-1]["t"] == bucket:
                    hourly[-1]["up"] += total_up
                    hourly[-1]["down"] += total_down
                else:
                    hourly.append({"t": bucket, "up": total_up, "down": total_down})
                while len(hourly) > 72:
                    hourly.pop(0)

            await store.mutate(_apply)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(2)


async def _housekeeping_loop():
    while True:
        try:
            await asyncio.sleep(30)
            def _check(db):
                for ib in db["inbounds"]:
                    st = inbound_status(ib)
                    if not st["live_enabled"] and ib.get("enabled", True):
                        pass
            await store.mutate(_check)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


async def _keep_alive_loop():
    await asyncio.sleep(15)
    while True:
        try:
            db = await store.get()
            interval = 600
            await asyncio.sleep(interval)
            if not (db.get("settings") or {}).get("keep_alive", True):
                continue
            port = os.environ.get("PANEL_PORT", "10000")
            async with httpx.AsyncClient(timeout=5) as client:
                await client.get(f"http://127.0.0.1:{port}/health")
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


# ------------------------------------------------------------------ health and doh
@app.get("/health")
async def health_check():
    return {"status": "ok", "version": APP_VERSION}


@app.api_route("/dns-query", methods=["GET", "POST", "OPTIONS"])
async def doh_endpoint(request: Request):
    """
    RFC 8484 compliant DNS-over-HTTPS (DoH) proxy.
    Handles GET wireformat/JSON queries and POST binary dns-message.
    """
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Accept",
            }
        )

    accept = request.headers.get("accept", "application/dns-message")
    content_type = request.headers.get("content-type", "application/dns-message")
    req_headers = {"accept": accept}

    try:
        if request.method == "POST":
            req_headers["content-type"] = content_type
            body = await request.body()
            try:
                upstream_res = await doh_http_client.post(DOH_PRIMARY, content=body, headers=req_headers)
            except Exception:
                upstream_res = await doh_http_client.post(DOH_SECONDARY, content=body, headers=req_headers)
        else:
            params = dict(request.query_params)
            try:
                upstream_res = await doh_http_client.get(DOH_PRIMARY, params=params, headers=req_headers)
            except Exception:
                upstream_res = await doh_http_client.get(DOH_SECONDARY, params=params, headers=req_headers)

        res_content_type = upstream_res.headers.get("content-type", "application/dns-message")
        return Response(
            content=upstream_res.content,
            status_code=upstream_res.status_code,
            media_type=res_content_type,
            headers={
                "Cache-Control": upstream_res.headers.get("cache-control", "max-age=300"),
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Accept",
            }
        )
    except Exception:
        return Response(content=b"", status_code=502)


# ------------------------------------------------------------------ page routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    db = await store.get()
    if not db.get("admin"):
        return RedirectResponse("/setup")
    user = await current_username(request)
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = await store.get()
    if db.get("admin"):
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "setup.html", {"app_version": APP_VERSION})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    db = await store.get()
    if not db.get("admin"):
        return RedirectResponse("/setup")
    if await current_username(request):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "login.html", {"app_version": APP_VERSION})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    db = await store.get()
    if not db.get("admin"):
        return RedirectResponse("/setup")
    if not await current_username(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "dashboard.html", {
        "app_version": APP_VERSION,
        "panel_name": PANEL_NAME,
        "telegram_contact": TELEGRAM_CONTACT,
    })


@app.get("/status/{uid}", response_class=HTMLResponse)
async def status_page(request: Request, uid: str):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        return HTMLResponse("<h1>404</h1><p>Not found.</p>", status_code=404)
    return templates.TemplateResponse(request, "status.html", {
        "uid": uid, "app_version": APP_VERSION,
        "panel_name": PANEL_NAME,
        "telegram_contact": TELEGRAM_CONTACT,
    })


# ------------------------------------------------------------------ auth api
@app.get("/api/setup-status")
async def setup_status():
    db = await store.get()
    return {"needs_setup": not bool(db.get("admin"))}


@app.post("/api/setup")
async def api_setup(request: Request):
    payload = await request.json()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    db = await store.get()
    if db.get("admin"):
        raise HTTPException(400, "already-configured")
    if not re.match(r"^[a-zA-Z0-9_]{3,32}$", username):
        raise HTTPException(400, "invalid-username")
    if len(password) < 6:
        raise HTTPException(400, "weak-password")

    hp = hash_password(password)

    def _apply(db):
        db["admin"] = {
            "username": username,
            "password_hash": hp["hash"],
            "salt": hp["salt"],
            "created_at": time.time(),
        }

    db = await store.mutate(_apply)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, db, username)
    return resp


@app.post("/api/login")
async def api_login(request: Request):
    payload = await request.json()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = _client_ip(request)
    db = await store.get()

    attempts = db.get("login_attempts", {}).get(ip, {})
    if attempts.get("locked_until", 0) > time.time():
        remain = int(attempts["locked_until"] - time.time())
        raise HTTPException(429, f"locked:{remain}")

    admin = db.get("admin")
    ok = bool(admin) and admin["username"] == username and verify_password(password, admin["salt"], admin["password_hash"])

    def _record(db):
        la = db.setdefault("login_attempts", {})
        if ok:
            la.pop(ip, None)
        else:
            rec = la.setdefault(ip, {"count": 0, "locked_until": 0})
            rec["count"] += 1
            if rec["count"] >= LOGIN_MAX_ATTEMPTS:
                rec["locked_until"] = time.time() + LOGIN_LOCK_SECONDS
                rec["count"] = 0

    db = await store.mutate(_record)

    if not ok:
        raise HTTPException(401, "invalid-credentials")

    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, db, username)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = await current_username(request)
    db = await store.get()
    return {
        "logged_in": bool(user),
        "username": user,
        "settings": db.get("settings", {}),
        "app_version": APP_VERSION,
    }


@app.post("/api/change-password")
async def api_change_password(request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
    old_password = payload.get("old_password") or ""
    new_password = payload.get("new_password") or ""
    new_username = (payload.get("new_username") or "").strip()
    db = await store.get()
    admin = db["admin"]
    if not verify_password(old_password, admin["salt"], admin["password_hash"]):
        raise HTTPException(401, "wrong-old-password")
    if new_username and not re.match(r"^[a-zA-Z0-9_]{3,32}$", new_username):
        raise HTTPException(400, "invalid-username")
    if new_password and len(new_password) < 6:
        raise HTTPException(400, "weak-password")

    def _apply(db):
        if new_password:
            hp = hash_password(new_password)
            db["admin"]["password_hash"] = hp["hash"]
            db["admin"]["salt"] = hp["salt"]
        if new_username:
            db["admin"]["username"] = new_username

    db = await store.mutate(_apply)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, db, db["admin"]["username"])
    return resp


@app.post("/api/settings")
async def api_update_settings(request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
    allowed = {
        "lang", "theme", "public_domain", "keep_alive",
        "default_fingerprint", "default_alpn", "sni_override",
        "fragment_enabled", "fragment_packets", "fragment_length", "fragment_interval",
    }
    valid_fp = {"chrome", "ios", "firefox", "edge", "random"}
    valid_alpn = {"http/1.1", "h2,http/1.1", "h3,h2,http/1.1"}

    def _apply(db):
        s = db.setdefault("settings", {})
        for k, v in payload.items():
            if k not in allowed:
                continue
            if k == "default_fingerprint" and v not in valid_fp:
                continue
            if k == "default_alpn" and v not in valid_alpn:
                continue
            s[k] = v

    db = await store.mutate(_apply)
    return {"ok": True, "settings": db["settings"]}


# ------------------------------------------------------------------ inbounds api
def serialize_inbound(ib) -> dict:
    st = inbound_status(ib)
    out = dict(ib)
    out["active_ips"] = None
    out.update({"status": st})
    return out


@app.get("/api/inbounds")
async def api_list_inbounds(user: str = Depends(require_auth)):
    db = await store.get()
    return {"inbounds": [serialize_inbound(ib) for ib in db["inbounds"]]}


@app.post("/api/inbounds")
async def api_create_inbound(request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
    db = await store.get()
    name = (payload.get("name") or "User").strip()[:64]
    quota_gb = float(payload.get("quota_gb") or 0)
    expire_days = int(payload.get("expire_days") or 0)
    max_connections = int(payload.get("max_connections") or 0)
    max_requests = int(payload.get("max_requests") or 0)
    fp = payload.get("fp") or (db.get("settings") or {}).get("default_fingerprint", "chrome")
    strict_single_ip = bool(payload.get("strict_single_ip") or False)

    ib = {
        "uid": gen_uid(),
        "uuid": gen_uuid(),
        "name": name,
        "enabled": True,
        "created_at": time.time(),
        "expire_days": expire_days,
        "expire_at": (time.time() + expire_days * 86400) if expire_days > 0 else None,
        "quota_gb": quota_gb,
        "max_connections": max_connections,
        "max_requests": max_requests,
        "request_count": 0,
        "used_up": 0,
        "used_down": 0,
        "fp": fp,
        "strict_single_ip": strict_single_ip,
        "note": payload.get("note", "")[:200] if payload.get("note") else "",
    }

    def _apply(db):
        db["inbounds"].append(ib)

    db = await store.mutate(_apply)
    xray_manager.generate_xray_config(db["inbounds"])
    xray_manager.restart_xray()
    return {"ok": True, "inbound": serialize_inbound(ib)}


@app.patch("/api/inbounds/{uid}")
async def api_update_inbound(uid: str, request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
    editable = {"name", "enabled", "quota_gb", "expire_days", "max_connections",
                "max_requests", "fp", "strict_single_ip", "note"}
    updated = {}

    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        for k, v in payload.items():
            if k in editable:
                ib[k] = v
        if "expire_days" in payload:
            days = int(payload["expire_days"] or 0)
            ib["expire_at"] = (ib["created_at"] + days * 86400) if days > 0 else None
        updated.update(ib)

    db = await store.mutate(_apply)
    xray_manager.generate_xray_config(db["inbounds"])
    xray_manager.restart_xray()
    return {"ok": True, "inbound": serialize_inbound(updated)}


@app.delete("/api/inbounds/{uid}")
async def api_delete_inbound(uid: str, user: str = Depends(require_auth)):
    found = {"v": False}

    def _apply(db):
        before = len(db["inbounds"])
        db["inbounds"] = [ib for ib in db["inbounds"] if ib["uid"] != uid]
        found["v"] = len(db["inbounds"]) != before

    await store.mutate(_apply)
    runtime["active"].pop(uid, None)
    if not found["v"]:
        raise HTTPException(404, "not-found")
        
    # Re-fetch db to get the updated inbounds
    db = await store.get()
    xray_manager.generate_xray_config(db["inbounds"])
    xray_manager.restart_xray()
    return {"ok": True}


@app.post("/api/inbounds/{uid}/reset-usage")
async def api_reset_usage(uid: str, user: str = Depends(require_auth)):
    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        ib["used_up"] = 0
        ib["used_down"] = 0
        ib["request_count"] = 0

    db = await store.mutate(_apply)
    return {"ok": True, "inbound": serialize_inbound(inbound_by_uid(db, uid))}


@app.post("/api/inbounds/{uid}/regenerate")
async def api_regenerate_uuid(uid: str, user: str = Depends(require_auth)):
    """Anti-resale: instantly revoke old links by rotating the VLESS uuid."""
    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        ib["uuid"] = gen_uuid()

    db = await store.mutate(_apply)
    runtime["active"].pop(uid, None)
    xray_manager.generate_xray_config(db["inbounds"])
    xray_manager.restart_xray()
    return {"ok": True, "inbound": serialize_inbound(inbound_by_uid(db, uid))}


def build_links(request: Request, db, ib) -> dict:
    """
    Build VLESS and VMess TLS links for WS and XHTTP.
    All non-TLS and Trojan configs are excluded.
    XHTTP strictly uses alpn=h2.
    """
    host = public_host(request, db)
    uuidv = ib["uuid"]
    name = ib["name"]
    fp = ib.get("fp") or (db.get("settings") or {}).get("default_fingerprint", "chrome")
    alpn = (db.get("settings") or {}).get("default_alpn", "http/1.1")
    sni = (db.get("settings") or {}).get("sni_override") or host
    port_tls = 443

    # 1. VLESS WS (TLS)
    vl_ws_tls = f"vless://{uuidv}@{host}:{port_tls}?encryption=none&security=tls&type=ws&host={quote(host)}&path={quote('/vl-ws', safe='/')}&sni={quote(sni)}&fp={fp}&alpn={quote(alpn, safe=',/')}#{quote(f'StanNG-{name}-VL-WS-TLS')}"

    # 2. VMess WS (TLS)
    def make_vmess(port, tls_mode, remark):
        vm_json = {
            "v": "2", "ps": remark, "add": host, "port": port, "id": uuidv,
            "aid": "0", "scy": "auto", "net": "ws", "type": "none",
            "host": host, "path": "/vm-ws", "tls": tls_mode, "sni": sni, "alpn": alpn
        }
        b64 = base64.b64encode(json.dumps(vm_json).encode()).decode()
        return f"vmess://{b64}"
    
    vm_ws_tls = make_vmess(port_tls, "tls", f"StanNG-{name}-VM-WS-TLS")

    # 3. VLESS XHTTP (TLS) - ALPN strictly set to h2
    vl_xh_tls = f"vless://{uuidv}@{host}:{port_tls}?encryption=none&security=tls&type=xhttp&host={quote(host)}&path={quote('/vl-xhttp', safe='/')}&sni={quote(sni)}&fp={fp}&alpn=h2#{quote(f'StanNG-{name}-VL-XHTTP-TLS')}"

    # Info configs (display-only) with distinct dummy endpoints to prevent client database collisions
    st = inbound_status(ib)
    quota_gb = ib.get("quota_gb") or 0
    used_gb = st["used"] / (1024 ** 3)
    quota_txt = f"{used_gb:.2f}/{quota_gb:g}GB" if quota_gb > 0 else f"{used_gb:.2f}GB used"
    days_txt = f"{st['days_left']}d left" if ib.get("expire_at") else "no expiry"
    status_remark = f"📊 {quota_txt} | ⏳ {days_txt}"
    free_remark = "StanNG Multi-Protocol ❤️"

    dummy_uuid_status = "00000000-0000-0000-0000-000000000001"
    dummy_uuid_credit = "00000000-0000-0000-0000-000000000002"
    dummy_link_status = f"vless://{dummy_uuid_status}@127.0.0.1:10001?encryption=none&security=none&type=tcp&headerType=none#{quote(status_remark)}"
    dummy_link_credit = f"vless://{dummy_uuid_credit}@127.0.0.1:10002?encryption=none&security=none&type=tcp&headerType=none#{quote(free_remark)}"
    info_configs = [
        {"remark": status_remark, "link": dummy_link_status, "kind": "status"},
        {"remark": free_remark, "link": dummy_link_credit, "kind": "credit"},
    ]

    all_links = [
        vl_ws_tls,
        vm_ws_tls,
        vl_xh_tls
    ]

    return {
        "tls": vl_ws_tls,  # Keep primary for QR code
        "all_links": all_links,
        "info_configs": info_configs,
    }


@app.get("/api/inbounds/{uid}/links")
async def api_inbound_links(uid: str, request: Request, user: str = Depends(require_auth)):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    host = public_host(request, db)
    scheme = get_scheme(request)  # Always use https in production
    links = build_links(request, db, ib)
    return {
        "links": links,
        "sub_url": f"{scheme}://{host}/sub/{uid}",
        "sub_json_url": f"{scheme}://{host}/sub/{uid}/json",
        "status_url": f"{scheme}://{host}/status/{uid}",
        "doh_url": f"{scheme}://{host}/dns-query",
    }


@app.get("/api/inbounds/{uid}/qr")
async def api_inbound_qr(uid: str, request: Request, user: str = Depends(require_auth)):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    links = build_links(request, db, ib)
    img = qrcode.make(links["tls"], border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ------------------------------------------------------------------ subscriptions

@app.get("/sub/{uid}")
async def sub_plain(uid: str, request: Request):
    """
    Returns base64-encoded subscription links with dynamic info configs and standard headers.
    Includes fresh Subscription-Userinfo and cache prevention headers.
    """
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    st = inbound_status(ib)
    links = build_links(request, db, ib)
    
    # Combine links: Info Configs (display-only) + all protocol links
    combined = (
        [c["link"] for c in links["info_configs"]]
        + links["all_links"]
    )
    raw = "\n".join(combined)
    
    # Base64 encode for wider client compatibility
    b64 = base64.b64encode(raw.encode()).decode()
    
    used_up = int(ib.get("used_up") or 0)
    used_down = int(ib.get("used_down") or 0)
    total_bytes = int(st["quota_bytes"])
    expire_ts = int(ib.get("expire_at") or 0)
    user_info_header = f"upload={used_up}; download={used_down}; total={total_bytes}; expire={expire_ts}"

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Subscription-Userinfo": user_info_header,
        "subscription-userinfo": user_info_header,
        "Profile-Update-Interval": "1",
        "profile-update-interval": "1",
        "Profile-Title": f"base64:{base64.b64encode(ib['name'].encode()).decode()}",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Powered-By": "StanNG",
    }
    return Response(content=b64, media_type="text/plain", headers=headers)


@app.get("/sub/{uid}/json")
async def sub_json(uid: str, request: Request):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    st = inbound_status(ib)
    links = build_links(request, db, ib)
    used_up = int(ib.get("used_up") or 0)
    used_down = int(ib.get("used_down") or 0)
    total_bytes = int(st["quota_bytes"])
    expire_ts = int(ib.get("expire_at") or 0)
    user_info_header = f"upload={used_up}; download={used_down}; total={total_bytes}; expire={expire_ts}"
    headers = {
        "Subscription-Userinfo": user_info_header,
        "subscription-userinfo": user_info_header,
        "Profile-Update-Interval": "1",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Powered-By": "StanNG",
    }
    return JSONResponse({
        "name": ib["name"],
        "uid": uid,
        "enabled": st["live_enabled"],
        "quota_gb": ib.get("quota_gb"),
        "used_gb": round(st["used"] / (1024 ** 3), 3),
        "used_bytes": st["used"],
        "quota_bytes": st["quota_bytes"],
        "days_left": st["days_left"],
        "expire_at": ib.get("expire_at"),
        "max_connections": ib.get("max_connections"),
        "active_connections": st["active_connections"],
        "links": {
            "tls": links["tls"],
            "all_links": links["all_links"],
            "info_configs": links["info_configs"],
        },
    }, headers=headers)


@app.get("/api/inbounds/{uid}/sub")
async def api_inbound_sub_alias(uid: str, request: Request, user: str = Depends(require_auth)):
    return await sub_json(uid, request)


# ------------------------------------------------------------------ public status api
@app.get("/api/status/{uid}")
async def api_public_status(uid: str):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    st = inbound_status(ib)
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }
    return JSONResponse({
        "name": ib["name"],
        "enabled": st["live_enabled"],
        "quota_gb": ib.get("quota_gb"),
        "used_gb": round(st["used"] / (1024 ** 3), 4),
        "used_bytes": st["used"],
        "quota_bytes": st["quota_bytes"],
        "days_left": st["days_left"],
        "expire_at": ib.get("expire_at"),
        "max_connections": ib.get("max_connections"),
        "active_connections": st["active_connections"],
        "max_requests": ib.get("max_requests"),
        "request_count": ib.get("request_count"),
    }, headers=headers)


# ------------------------------------------------------------------ system / stats
@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}


@app.get("/stats")
async def stats(request: Request, user: str = Depends(require_auth)):
    db = await store.get()
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    started = db["stats"].get("started_at", time.time())
    uptime = time.time() - started

    colo = "?"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
            for line in r.text.splitlines():
                if line.startswith("colo="):
                    colo = line.split("=", 1)[1]
    except Exception:
        pass

    total_active = sum(len(v) for v in runtime["active"].values())

    # Build a continuous 24-hour timeline from (now - 23h) to current hour
    raw_hourly = {item["t"]: item for item in db["stats"].get("hourly", []) if "t" in item}
    now_bucket = int(time.time() // 3600) * 3600
    hourly_series = []
    for i in range(23, -1, -1):
        bucket_t = now_bucket - (i * 3600)
        if bucket_t in raw_hourly:
            hourly_series.append({
                "t": bucket_t,
                "up": raw_hourly[bucket_t].get("up", 0),
                "down": raw_hourly[bucket_t].get("down", 0)
            })
        else:
            hourly_series.append({"t": bucket_t, "up": 0, "down": 0})

    return {
        "cpu_percent": cpu,
        "mem_percent": mem.percent,
        "mem_used_mb": round(mem.used / 1024 / 1024, 1),
        "mem_total_mb": round(mem.total / 1024 / 1024, 1),
        "uptime_seconds": uptime,
        "total_up": db["stats"].get("total_up", 0),
        "total_down": db["stats"].get("total_down", 0),
        "hourly": hourly_series,
        "inbounds_count": len(db["inbounds"]),
        "active_connections": total_active,
        "location": describe_colo(colo),
    }


def _ver_tuple(v):
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else (0,)


async def _resolve_latest_release(repo: str, current: str, client: httpx.AsyncClient):
    """Returns (latest_version, html_url, download_zip_url) using GitHub's releases/tags API."""
    latest, url, zip_url = current, f"https://github.com/{repo}/releases", None
    try:
        r = await client.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=OTA_HEADERS)
        if r.status_code == 200:
            data = r.json()
            tag = (data.get("tag_name") or "").lstrip("v")
            if tag:
                latest = tag
                url = data.get("html_url", url)
                zip_url = data.get("zipball_url") or f"https://api.github.com/repos/{repo}/zipball/{data.get('tag_name')}"
        else:
            r2 = await client.get(f"https://api.github.com/repos/{repo}/tags", headers=OTA_HEADERS)
            if r2.status_code == 200 and r2.json():
                tags = r2.json()
                if tags:
                    tag_info = tags[0]
                    tag_name = tag_info.get("name") or current
                    latest = tag_name.lstrip("v")
                    url = f"https://github.com/{repo}/releases/tag/{tag_name}"
                    zip_url = tag_info.get("zipball_url") or f"https://api.github.com/repos/{repo}/zipball/{tag_name}"
    except Exception:
        pass
    return latest, url, zip_url


@app.get("/api/ota/check")
async def api_ota_check(user: str = Depends(require_auth)):
    current = APP_VERSION
    latest = current
    url = f"https://github.com/{OTA_REPO}/releases"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            latest, url, _zip = await _resolve_latest_release(OTA_REPO, current, client)
    except Exception:
        pass

    update_available = _ver_tuple(latest) > _ver_tuple(current)
    return {"current": current, "latest": latest, "update_available": update_available, "url": url}


# ------------------------------------------------------------------ OTA self-update
UPDATE_LOCK = asyncio.Lock()
NEVER_TOUCH = {"data"}


def _safe_extract_zip(zip_path: str, dest_dir: str):
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError("empty archive")
        root_prefix = names[0].split("/")[0] + "/"
        for member in names:
            if not member.startswith(root_prefix):
                continue
            rel = member[len(root_prefix):]
            if not rel:
                continue
            target = os.path.normpath(os.path.join(dest_dir, rel))
            if not target.startswith(os.path.normpath(dest_dir) + os.sep) and target != os.path.normpath(dest_dir):
                raise RuntimeError(f"unsafe path in archive: {member}")
            if member.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())


def _apply_staged_update(staged_dir: str, live_dir: str) -> list:
    import shutil
    touched = []
    for entry in os.listdir(staged_dir):
        if entry in NEVER_TOUCH:
            continue
        src = os.path.join(staged_dir, entry)
        dst = os.path.join(live_dir, entry)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        touched.append(entry)
    return touched


@app.post("/api/ota/update")
async def api_ota_update(request: Request, user: str = Depends(require_auth)):
    if UPDATE_LOCK.locked():
        raise HTTPException(409, "update-already-in-progress")

    async with UPDATE_LOCK:
        import tempfile
        import shutil as _shutil

        current = APP_VERSION
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                latest, html_url, zip_url = await _resolve_latest_release(OTA_REPO, current, client)
                if _ver_tuple(latest) <= _ver_tuple(current):
                    return {"ok": False, "reason": "already-up-to-date", "current": current, "latest": latest}
                if not zip_url:
                    zip_url = f"https://api.github.com/repos/{OTA_REPO}/zipball/{latest}"

                tmp_root = tempfile.mkdtemp(prefix="stanng_ota_")
                zip_path = os.path.join(tmp_root, "release.zip")
                staged_dir = os.path.join(tmp_root, "staged")
                os.makedirs(staged_dir, exist_ok=True)

                async with client.stream("GET", zip_url, headers=OTA_HEADERS) as resp:
                    if resp.status_code != 200:
                        raise HTTPException(502, f"download-failed-{resp.status_code}")
                    with open(zip_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)

            _safe_extract_zip(zip_path, staged_dir)

            if not os.path.exists(os.path.join(staged_dir, "main.py")):
                _shutil.rmtree(tmp_root, ignore_errors=True)
                raise HTTPException(502, "downloaded-archive-missing-main.py")

            staged_data = os.path.join(staged_dir, "data")
            if os.path.isdir(staged_data):
                _shutil.rmtree(staged_data, ignore_errors=True)

            touched = _apply_staged_update(staged_dir, BASE_DIR)
            # 🛠️ تغییر باگ ۱: اگر هیچ فایلی کپی نشد، خطا بده
            if not touched:
                _shutil.rmtree(tmp_root, ignore_errors=True)
                raise HTTPException(502, "update-failed-no-files-copied")
            _shutil.rmtree(tmp_root, ignore_errors=True)

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"update-failed: {e}")

        async def _delayed_restart():
            await asyncio.sleep(1.5)
            os._exit(87)

        asyncio.create_task(_delayed_restart())
        return {
            "ok": True,
            "previous_version": current,
            "new_version": latest,
            "files_updated": touched,
            "restarting": True,
        }



if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PANEL_PORT", 10000))
    uvicorn.run("main:app", host="127.0.0.1", port=port, log_level="info")
