"""Basit Görev Yöneticisi sunucusu.

Sadece Python standart kütüphanesini kullanır. Her ekibin verisi kendi JSON
dosyasında tutulur; sunucu kapatılıp açıldığında kaldığı yerden devam eder.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Çalışma dosyalarının klasörü. Test için GOREV_HOME ile başka bir klasör verilebilir.
HOME_DIR = os.environ.get("GOREV_HOME") or BASE_DIR
PID_FILE = os.path.join(HOME_DIR, "server.pid")
LOG_FILE = os.path.join(HOME_DIR, "server.log")
SECRET_FILE = os.path.join(HOME_DIR, "secret.key")
BACKUP_DIR = os.path.join(HOME_DIR, "backups")
INDEX_FILE = os.path.join(BASE_DIR, "static", "index.html")
LOGIN_FILE = os.path.join(BASE_DIR, "static", "login.html")

HOST = "0.0.0.0"
PORT = int(os.environ.get("GOREV_PORT", "8080"))

# Ekipler (giriş bilgileri, üyeler, veri dosyası) teams.json dosyasından okunur.
# Bu dosya şifre içerdiği için repoya gönderilmez; örnek için teams.example.json'a bakın.
TEAMS_FILE = os.environ.get("GOREV_TEAMS") or os.path.join(BASE_DIR, "teams.json")
LEGACY_TEAM = "analitik"  # ekip bilgisi taşımayan eski oturum çerezleri bu ekibe aittir

SESSION_COOKIE = "gorev_oturum"
SESSION_SECONDS = 30 * 24 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 30
MAX_BACKUPS = 30
# Paylaşımlar yalnızca bellekte tutulur (veri dosyasına ve loga yazılmaz).
SHARE_TTL_SECONDS = 24 * 3600
SHARE_MAX_CHARS = 200_000
SHARE_MAX_FAILURES = 5

STATUSES = ("todo", "inprogress", "finished")
DATA_KEYS = ("projects", "tasks", "notifications", "deletedTasks")

# pythonw ile çalışırken konsol yoktur; çıktıları log dosyasına yönlendir.
if sys.stdout is None or sys.stderr is None:
    _log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_teams():
    # Her ekibin kendi girişi, üyeleri ve veri dosyası vardır; birbirlerini görmezler.
    # Bir ekibin şifresi değişirse o ekibin açık oturumları geçersiz olur.
    try:
        with open(TEAMS_FILE, encoding="utf-8") as f:
            teams = json.load(f)
    except FileNotFoundError:
        log("teams.json bulunamadı: teams.example.json dosyasını teams.json olarak kopyalayıp bilgileri doldurun.")
        sys.exit(1)
    for key, team in teams.items():
        if not team.get("username") or not team.get("password") or not team.get("users"):
            log(f"teams.json içindeki '{key}' ekibinin kullanıcı adı, şifresi veya üyeleri eksik.")
            sys.exit(1)
    return teams


TEAMS = load_teams()

_lock = threading.Lock()
_data = {team: {key: [] for key in DATA_KEYS} for team in TEAMS}
_failures = {}  # ip -> [hatalı deneme sayısı, kilit bitiş zamanı]
_secret = b""
_shares = {}  # paylaşım id -> şifreli içerik ve üst bilgi; sadece bellekte
_shares_lock = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


class Ctx:
    """İsteği yapan kişi ve ekibi."""

    def __init__(self, team, user):
        self.team = team
        self.user = user
        self.cfg = TEAMS[team]
        self.users = self.cfg["users"]
        self.data = _data[team]


# ---------- Veri ----------

def data_path(team):
    return os.path.join(HOME_DIR, TEAMS[team]["dataFile"])


def load_data():
    for team in TEAMS:
        path = data_path(team)
        if not os.path.exists(path):
            save_data(team)
            continue
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        _data[team] = {key: loaded.get(key, []) for key in DATA_KEYS}


def save_data(team):
    # Önce geçici dosyaya yaz, sonra değiştir: yazma sırasında kapanırsa veri bozulmaz.
    path = data_path(team)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data[team], f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def backup_data():
    # Her açılışta her ekibin veri dosyasını yedekle; ekip başına en yeni MAX_BACKUPS kopya tutulur.
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    for team in TEAMS:
        path = data_path(team)
        if not os.path.exists(path):
            continue
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stem = os.path.splitext(TEAMS[team]["dataFile"])[0]
        shutil.copy2(path, os.path.join(BACKUP_DIR, f"{stem}-{stamp}.json"))
        pattern = re.compile(rf"^{re.escape(stem)}-\d{{8}}-\d{{6}}\.json$")
        backups = sorted(f for f in os.listdir(BACKUP_DIR) if pattern.match(f))
        for old in backups[:-MAX_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, old))


# ---------- Oturum ----------

def load_secret():
    # Oturum çerezlerini imzalamak için anahtar; dosyada durduğu için yeniden başlatınca oturumlar düşmez.
    global _secret
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, encoding="ascii") as f:
            _secret = f.read().strip().encode()
    if not _secret:
        _secret = secrets.token_hex(32).encode()
        with open(SECRET_FILE, "w", encoding="ascii") as f:
            f.write(_secret.decode())


def sign(team, payload):
    key = _secret + TEAMS[team]["password"].encode("utf-8")
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_token(team, user):
    expires = int(time.time()) + SESSION_SECONDS
    identity = base64.urlsafe_b64encode(f"{team}:{user}".encode("utf-8")).decode().rstrip("=")
    payload = f"{expires}.{identity}"
    return f"{payload}.{sign(team, payload)}"


def session_of(token):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    expires, identity, signature = parts
    if not expires.isdigit() or int(expires) < time.time():
        return None
    try:
        decoded = base64.urlsafe_b64decode(identity + "=" * (-len(identity) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    team, _, user = decoded.partition(":") if ":" in decoded else (LEGACY_TEAM, "", decoded)
    if team not in TEAMS:
        return None
    if not hmac.compare_digest(signature, sign(team, f"{expires}.{identity}")):
        return None
    return Ctx(team, user) if user in TEAMS[team]["users"] else None


# ---------- İş kuralları ----------

class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def text_field(body, key, max_len, required=False, label=None):
    value = body.get(key) or ""
    if not isinstance(value, str):
        raise ApiError(f"{label or key} geçersiz")
    value = value.strip()
    if required and not value:
        raise ApiError(f"{label or key} zorunludur")
    if len(value) > max_len:
        raise ApiError(f"{label or key} en fazla {max_len} karakter olabilir")
    return value


def find_task(ctx, task_id):
    task = next((t for t in ctx.data["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise ApiError("Görev bulunamadı", 404)
    return task


def state_for(ctx):
    mine = sorted((n for n in ctx.data["notifications"] if n["user"] == ctx.user),
                  key=lambda n: n["at"], reverse=True)
    # Okunmamışların hepsi + en son 30 bildirim
    notifications = [n for i, n in enumerate(mine) if i < 30 or not n["read"]]
    return {
        "team": ctx.cfg["name"],
        "users": ctx.users,
        "me": ctx.user,
        "projects": ctx.data["projects"],
        "tasks": ctx.data["tasks"],
        "notifications": notifications,
        "shares": shares_for(ctx),
        "serverTime": now_ms(),
    }


def create_project(body, ctx):
    name = text_field(body, "name", 80, required=True, label="Proje adı")
    description = text_field(body, "description", 1000, label="Açıklama")
    if any(p["name"].casefold() == name.casefold() for p in ctx.data["projects"]):
        raise ApiError("Bu isimde bir proje zaten var")
    project = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "description": description,
        "createdAt": now_ms(),
        "createdBy": ctx.user,
    }
    ctx.data["projects"].append(project)
    return project


def task_fields(body, ctx):
    title = text_field(body, "title", 150, required=True, label="Görev adı")
    description = text_field(body, "description", 3000, label="Açıklama")
    deadline = text_field(body, "deadline", 10, label="Deadline")
    if deadline:
        try:
            datetime.strptime(deadline, "%Y-%m-%d")
        except ValueError:
            raise ApiError("Deadline tarihi geçersiz")
    project_id = body.get("projectId")
    if not any(p["id"] == project_id for p in ctx.data["projects"]):
        raise ApiError("Geçerli bir proje seçin")
    assignee = body.get("assignee") or None
    if assignee is not None and assignee not in ctx.users:
        raise ApiError("Atanan kişi geçersiz")
    return {
        "title": title,
        "description": description,
        "deadline": deadline,
        "projectId": project_id,
        "assignee": assignee,
    }


def notify_assignment(task, ctx):
    assignee = task.get("assignee")
    if not assignee or assignee == ctx.user:
        return
    ctx.data["notifications"].append({
        "id": uuid.uuid4().hex[:12],
        "user": assignee,
        "type": "assigned",
        "taskId": task["id"],
        "taskTitle": task["title"],
        "by": ctx.user,
        "at": now_ms(),
        "read": False,
    })


def create_task(body, ctx):
    fields = task_fields(body, ctx)
    status = body.get("status") or "todo"
    if status not in STATUSES:
        raise ApiError("Durum geçersiz")
    ts = now_ms()
    task = {
        "id": uuid.uuid4().hex[:12],
        **fields,
        "reporter": ctx.user,
        "status": status,
        "createdAt": ts,
        "statusSince": ts,
        "history": [
            {"from": None, "to": status, "user": ctx.user, "at": ts, "durationMs": None, "note": ""}
        ],
    }
    ctx.data["tasks"].append(task)
    notify_assignment(task, ctx)
    return task


def update_task(task_id, body, ctx):
    task = find_task(ctx, task_id)
    fields = task_fields(body, ctx)
    ts = now_ms()
    edit_keys = ("title", "description", "deadline", "projectId")
    if any(task.get(k, "") != fields[k] for k in edit_keys):
        for k in edit_keys:
            task[k] = fields[k]
        task["history"].append({"kind": "edit", "user": ctx.user, "at": ts})
    if fields["assignee"] != task.get("assignee"):
        task["assignee"] = fields["assignee"]
        task["history"].append({"kind": "assign", "user": ctx.user, "assignee": fields["assignee"], "at": ts})
        notify_assignment(task, ctx)
    return task


def delete_task(task_id, _body, ctx):
    task = find_task(ctx, task_id)
    ctx.data["tasks"].remove(task)
    # Tamamen kaybolmasın diye arşive taşınır (arayüzde görünmez).
    ctx.data["deletedTasks"].append({**task, "deletedBy": ctx.user, "deletedAt": now_ms()})
    ctx.data["notifications"] = [n for n in ctx.data["notifications"] if n["taskId"] != task_id]
    log(f"Görev silindi [{ctx.team}]: {task['title']!r} ({ctx.user})")
    return {"ok": True, "id": task_id}


def move_task(task_id, body, ctx):
    task = find_task(ctx, task_id)
    status = body.get("status")
    if status not in STATUSES:
        raise ApiError("Yeni durum geçersiz")
    if status == task["status"]:
        raise ApiError("Görev zaten bu durumda")
    user = body.get("user")
    if user not in ctx.users:
        raise ApiError("Kim yaptı seçilmelidir")
    note = text_field(body, "note", 200, label="Süre notu")

    ts = now_ms()
    task["history"].append({
        "from": task["status"],
        "to": status,
        "user": user,
        "at": ts,
        "durationMs": max(0, ts - task["statusSince"]),
        "note": note,
    })
    task["status"] = status
    task["statusSince"] = ts
    return task


def mark_read(body, ctx):
    mark_all = body.get("all") is True
    ids = body.get("ids") or []
    if not mark_all and not isinstance(ids, list):
        raise ApiError("Geçersiz istek")
    wanted = set(ids)
    ts = now_ms()
    count = 0
    for n in ctx.data["notifications"]:
        if n["user"] == ctx.user and not n["read"] and (mark_all or n["id"] in wanted):
            n["read"] = True
            n["readAt"] = ts
            count += 1
    return {"ok": True, "count": count}


# ---------- Paylaş ----------
# İçerik, gönderenin belirlediği şifreden türetilen anahtarla şifrelenir
# (PBKDF2 + HMAC-SHA256 akış şifresi + bütünlük etiketi). Şifre sunucuda saklanmaz;
# şifre bilinmeden içerik bellekte bile okunamaz.

def _share_keys(password, salt):
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000, dklen=64)
    return key[:32], key[32:]


def _keystream_xor(key, nonce, data):
    out = bytearray(len(data))
    for start in range(0, len(data), 32):
        block = hmac.new(key, nonce + (start // 32).to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[start:start + 32]
        out[start:start + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


def encrypt_share(text, password):
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _share_keys(password, salt)
    cipher = _keystream_xor(enc_key, nonce, text.encode("utf-8"))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return {"salt": salt, "nonce": nonce, "cipher": cipher, "tag": tag}


def decrypt_share(box, password):
    enc_key, mac_key = _share_keys(password, box["salt"])
    tag = hmac.new(mac_key, box["nonce"] + box["cipher"], hashlib.sha256).digest()
    if not hmac.compare_digest(tag, box["tag"]):
        return None  # şifre yanlış
    return _keystream_xor(enc_key, box["nonce"], box["cipher"]).decode("utf-8")


def _purge_shares():
    cutoff = now_ms() - SHARE_TTL_SECONDS * 1000
    for sid in [sid for sid, s in _shares.items() if s["at"] < cutoff]:
        del _shares[sid]


def shares_for(ctx):
    with _shares_lock:
        _purge_shares()
        team_shares = [(sid, s) for sid, s in _shares.items() if s["team"] == ctx.team]
        incoming = [{"id": sid, "from": s["from"], "at": s["at"], "chars": s["chars"]}
                    for sid, s in team_shares if s["to"] == ctx.user]
        outgoing = [{"id": sid, "to": s["to"], "at": s["at"]}
                    for sid, s in team_shares if s["from"] == ctx.user]
    incoming.sort(key=lambda s: s["at"], reverse=True)
    outgoing.sort(key=lambda s: s["at"], reverse=True)
    return {"incoming": incoming, "outgoing": outgoing}


def create_share(body, ctx):
    to = body.get("to")
    if to not in ctx.users or to == ctx.user:
        raise ApiError("Geçerli bir alıcı seçin")
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError("Paylaşılacak içerik boş olamaz")
    if len(content) > SHARE_MAX_CHARS:
        raise ApiError(f"İçerik en fazla {SHARE_MAX_CHARS:,} karakter olabilir".replace(",", "."))
    password = body.get("password")
    if not isinstance(password, str) or not 4 <= len(password) <= 100:
        raise ApiError("Şifre en az 4 karakter olmalı")
    box = encrypt_share(content, password)
    sid = secrets.token_urlsafe(9)
    with _shares_lock:
        _shares[sid] = {**box, "team": ctx.team, "from": ctx.user, "to": to, "at": now_ms(),
                        "chars": len(content), "fails": 0, "lockedUntil": 0}
    return {"ok": True, "id": sid}


def _own_incoming(sid, ctx):
    share = _shares.get(sid)
    if share and share["team"] == ctx.team and share["to"] == ctx.user:
        return share
    return None


def open_share(sid, body, ctx):
    password = body.get("password") if isinstance(body.get("password"), str) else ""
    with _shares_lock:
        share = _own_incoming(sid, ctx)
        if not share:
            raise ApiError("Paylaşım bulunamadı; silinmiş ya da süresi dolmuş olabilir", 404)
        wait = share["lockedUntil"] - time.time()
        if wait > 0:
            raise ApiError(f"Çok fazla hatalı deneme. {int(wait) + 1} sn sonra tekrar deneyin.", 429)
        box = dict(share)
    content = decrypt_share(box, password)  # yavaş işlem; kilit dışında
    if content is None:
        with _shares_lock:
            share = _shares.get(sid)
            if share:
                share["fails"] += 1
                if share["fails"] >= SHARE_MAX_FAILURES:
                    share["fails"] = 0
                    share["lockedUntil"] = time.time() + LOCK_SECONDS
        raise ApiError("Şifre hatalı", 403)
    return {"id": sid, "from": box["from"], "at": box["at"], "content": content}


def finish_share(sid, _body, ctx):
    # Alıcı "Kapat" dediğinde paylaşım bellekten silinir.
    with _shares_lock:
        if _own_incoming(sid, ctx):
            del _shares[sid]
    return {"ok": True}


def cancel_share(sid, _body, ctx):
    # Gönderen, alınmamış paylaşımı geri çekebilir.
    with _shares_lock:
        share = _shares.get(sid)
        if share and share["team"] == ctx.team and share["from"] == ctx.user:
            del _shares[sid]
    return {"ok": True}


# ---------- HTTP ----------

TASK_ACTIONS = {"move": move_task, "update": update_task, "delete": delete_task}
SHARE_ACTIONS = {"open": open_share, "done": finish_share, "cancel": cancel_share}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload, headers=None):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    def _html(self, file_path):
        with open(file_path, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _redirect(self, location):
        self._send(302, b"", "text/plain", {"Location": location})

    def _session(self):
        try:
            morsel = SimpleCookie(self.headers.get("Cookie") or "").get(SESSION_COOKIE)
        except CookieError:
            return None
        return session_of(morsel.value) if morsel is not None else None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        ctx = self._session()
        if path == "/login":
            return self._redirect("/") if ctx else self._html(LOGIN_FILE)
        if path in ("/", "/index.html"):
            return self._html(INDEX_FILE) if ctx else self._redirect("/login")
        if path == "/api/state":
            if not ctx:
                return self._json(401, {"error": "Oturum açılmamış"})
            with _lock:
                body = json.dumps(state_for(ctx), ensure_ascii=False).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        self._json(404, {"error": "Bulunamadı"})

    def _login(self, body):
        ip = self.client_address[0]
        now = time.time()
        with _lock:
            count, locked_until = _failures.get(ip, [0, 0])
            if locked_until > now:
                raise ApiError(f"Çok fazla hatalı deneme. {int(locked_until - now) + 1} sn sonra tekrar deneyin.", 429)
            username = body.get("username") if isinstance(body.get("username"), str) else ""
            password = body.get("password") if isinstance(body.get("password"), str) else ""
            # Kullanıcı adı hangi ekibe aitse o ekip açılır.
            team = next((key for key, t in TEAMS.items()
                         if hmac.compare_digest(username.strip().encode("utf-8"), t["username"].encode("utf-8"))), None)
            ok = team is not None and hmac.compare_digest(password.encode("utf-8"), TEAMS[team]["password"].encode("utf-8"))
            if not ok:
                count += 1
                _failures[ip] = [0, now + LOCK_SECONDS] if count >= MAX_FAILURES else [count, 0]
                log(f"Hatalı giriş denemesi: {ip}")
                raise ApiError("Kullanıcı adı veya şifre hatalı", 401)
            _failures.pop(ip, None)

        cfg = TEAMS[team]
        # 1. adım: şifre doğru, "sen kimsin?" için ekibin isim listesini döndür
        user = body.get("user")
        if not user:
            return self._json(200, {"ok": True, "team": cfg["name"], "users": cfg["users"]})
        # 2. adım: isim seçildi, oturumu aç
        if user not in cfg["users"]:
            raise ApiError("Kullanıcı geçersiz")
        cookie = f"{SESSION_COOKIE}={make_token(team, user)}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Lax"
        log(f"Giriş [{team}]: {user} ({ip})")
        self._json(200, {"ok": True}, {"Set-Cookie": cookie})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ApiError("İstek çok büyük", 413)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise ApiError("Geçersiz istek")
            if not isinstance(body, dict):
                raise ApiError("Geçersiz istek")

            if path == "/api/login":
                return self._login(body)
            if path == "/api/logout":
                cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                return self._json(200, {"ok": True}, {"Set-Cookie": cookie})
            ctx = self._session()
            if not ctx:
                raise ApiError("Oturum açılmamış", 401)

            parts = path.strip("/").split("/")

            # Paylaşımlar veri dosyasına yazılmaz, bu yüzden kayıt bloğunun dışında ele alınır.
            if path == "/api/shares":
                return self._json(200, create_share(body, ctx))
            if len(parts) == 4 and parts[:2] == ["api", "shares"] and parts[3] in SHARE_ACTIONS:
                return self._json(200, SHARE_ACTIONS[parts[3]](parts[2], body, ctx))

            with _lock:
                if path == "/api/projects":
                    result = create_project(body, ctx)
                elif path == "/api/tasks":
                    result = create_task(body, ctx)
                elif path == "/api/notifications/read":
                    result = mark_read(body, ctx)
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in TASK_ACTIONS:
                    result = TASK_ACTIONS[parts[3]](parts[2], body, ctx)
                else:
                    raise ApiError("Bulunamadı", 404)
                save_data(ctx.team)
            self._json(200, result)
        except ApiError as exc:
            self._json(exc.code, {"error": str(exc)})
        except Exception as exc:  # beklenmeyen hata: logla, sunucu çalışmaya devam etsin
            log(f"Hata: {exc!r}")
            self._json(500, {"error": "Sunucu hatası"})


def main():
    try:
        load_secret()
        backup_data()
        load_data()
    except Exception as exc:
        log(f"Başlatma hatası, sunucu açılmadı: {exc!r}")
        sys.exit(1)
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        log(f"Port {PORT} açılamadı: {exc}")
        sys.exit(1)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    log(f"Görev Yöneticisi çalışıyor: http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
