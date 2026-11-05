#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iOS 风格积分商城 —— 纯标准库实现，零外部依赖。
用户端：注册 / 登录（密码+验证码） / 自研人机验证 / 60秒发码限频 / 邮箱真发信 /
       每日签到 / 商品兑换 / 兑换记录 / 积分明细
管理端：概览统计 / 上架·编辑·删除商品 / 卡密批量补货 / 用户管理（增减积分·封禁·删除）/ 全站订单
部署：Cloudflare 免登录临时隧道 + GitHub Actions 云端计时（见 .github/workflows）
运行：python3 server.py  （SMTP / 管理员 配置走环境变量，见 .env.example）
"""

import json
import os
import re
import random
import hashlib
import threading
import datetime
import secrets
import smtplib
import ssl
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8000"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DATA_FILE = os.path.join(BASE_DIR, "data.json")


def _load_env_file():
    """读取项目根目录 .env（若存在）。不覆盖已有环境变量，GitHub Secrets 优先。"""
    p = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()

# ---------- SMTP 配置（仅来自环境变量，绝不硬编码） ----------
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yeah.net")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")   # 填邮箱的「授权码」
SMTP_FROM = os.environ.get("SMTP_FROM", EMAIL_USER)
DEV_MODE = (not EMAIL_PASS)  # 未配置授权码时，开发模式直接回显验证码，方便测试

# ---------- 管理员种子（仅来自环境变量，绝不硬编码） ----------
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# ---------- 业务常量 ----------
DEFAULT_PRODUCTS = [
    {"id": "p1", "name": "星巴克中杯拿铁", "icon": "cup", "cost": 300, "desc": "香醇拿铁一杯", "stock": 99, "type": "normal", "codes": []},
    {"id": "p2", "name": "视频会员月卡", "icon": "ticket", "cost": 800, "desc": "热门平台月卡", "stock": 50, "type": "normal", "codes": []},
    {"id": "p3", "name": "蓝牙耳机", "icon": "headphones", "cost": 2000, "desc": "无线降噪耳机", "stock": 10, "type": "normal", "codes": []},
    {"id": "p4", "name": "京东 50 元券", "icon": "tag", "cost": 500, "desc": "满减无门槛", "stock": 30, "type": "normal", "codes": []},
    {"id": "p5", "name": "机械键盘", "icon": "keyboard", "cost": 3500, "desc": "客制化手感", "stock": 5, "type": "normal", "codes": []},
    {"id": "p6", "name": "定制帆布包", "icon": "bag", "cost": 600, "desc": "潮流联名款", "stock": 40, "type": "normal", "codes": []},
]
DEFAULT_IDS = {p["id"] for p in DEFAULT_PRODUCTS}
CHECKIN_BASE = 10
CHECKIN_STREAK_BONUS = 5
START_POINTS = 1200
CODE_TTL = 300            # 验证码有效期 5 分钟
CODE_COOLDOWN = 60        # 发码冷却 60 秒，防风控
CAPTCHA_TTL = 300         # 人机验证通过票据有效期 5 分钟
SESSION_TTL = 86400 * 7   # 登录会话 7 天
CAPTCHA_POOL = ["bag", "cup", "gift", "headphones", "phone", "keyboard",
                "box", "cart", "crown", "bolt", "calendar", "star"]

_lock = threading.Lock()
SESSIONS = {}      # sid -> {"email":, "exp":}
CODES = {}         # email -> {"code":, "exp":, "last":}
CAPTCHAS = {}      # token -> [图标名,...]
CAPTCHA_PASS = {}  # pass_token -> exp


def now_ts():
    return datetime.datetime.now().timestamp()


def today_str():
    return datetime.date.today().isoformat()


def is_email(s):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s or ""))


# ---------- 密码哈希（标准库） ----------
def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 100000)
    return salt.hex() + ":" + h.hex()


def verify_pw(pw, stored):
    try:
        salt_hex, h_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 100000)
        return h.hex() == h_hex
    except Exception:
        return False


# ---------- 数据持久化 ----------
def _fresh_data():
    return {"products": [dict(p, codes=[]) for p in DEFAULT_PRODUCTS],
            "users": {}, "redeem_codes": [], "deleted_defaults": []}


def load_data():
    if not os.path.exists(DATA_FILE):
        return _fresh_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("products", [])
        data.setdefault("users", {})
        data.setdefault("redeem_codes", [])
        data.setdefault("deleted_defaults", [])
        # 合并缺失的默认商品（兜底：避免普通用户没有任何买得起的商品），
        # 但跳过「被管理员主动删除」的默认商品，否则删除会被刷新复活。
        deleted = set(data["deleted_defaults"])
        existing_ids = {p.get("id") for p in data["products"] if isinstance(p, dict)}
        for dp in DEFAULT_PRODUCTS:
            if dp["id"] not in existing_ids and dp["id"] not in deleted:
                data["products"].append(dict(dp, codes=[]))
                existing_ids.add(dp["id"])
        for u in data["users"].values():
            u.setdefault("diamonds", 0)
            u.setdefault("emeralds", 0)
            u.setdefault("points_log", [])
            u.setdefault("banned", False)
        return data
    except Exception:
        return _fresh_data()


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def new_user(role="user"):
    return {"points": START_POINTS, "diamonds": 0, "emeralds": 0,
            "last_checkin": None, "checkin_streak": 0,
            "exchanges": [], "tasks_done": {}, "pw": None, "role": role,
            "points_log": [], "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "banned": False}


def ensure_admin():
    """以环境变量中的账号密码种子化管理员。"""
    if not (ADMIN_EMAIL and ADMIN_PASSWORD):
        return
    with _lock:
        data = load_data()
        u = data["users"].get(ADMIN_EMAIL)
        if not u:
            u = new_user("admin")
            data["users"][ADMIN_EMAIL] = u
        u["role"] = "admin"
        u["pw"] = hash_pw(ADMIN_PASSWORD)
        u.setdefault("points_log", [])
        u.setdefault("created_at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        u["banned"] = u.get("banned", False)
        data["users"][ADMIN_EMAIL] = u
        save_data(data)


def get_user(email):
    return load_data()["users"].get(email)


def log_points(data, email, delta, note):
    """在 data 上给 email 记一条积分变动（调用方需持锁并已改 points）。"""
    u = data["users"][email]
    u.setdefault("points_log", [])
    u["points_log"].insert(0, {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "delta": delta, "balance": u["points"], "note": note,
    })
    if len(u["points_log"]) > 200:
        u["points_log"] = u["points_log"][:200]


# ---------- 邮件发送 ----------
def send_code_email(to_email, code):
    if DEV_MODE:
        return
    msg = MIMEText(
        f"您正在使用「积分商城」。\n\n验证码：{code}\n有效期 5 分钟，请勿泄露给他人。\n\n如非本人操作，请忽略本邮件。",
        "plain", "utf-8")
    msg["Subject"] = "积分商城 · 邮箱验证码"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
        s.login(EMAIL_USER, EMAIL_PASS)
        s.send_message(msg)


# ---------- 人机验证 ----------
def make_captcha():
    targets = random.sample(CAPTCHA_POOL, 3)
    distract = random.sample([e for e in CAPTCHA_POOL if e not in targets], 6)
    grid = targets + distract
    random.shuffle(grid)
    token = secrets.token_hex(16)
    CAPTCHAS[token] = targets
    return {"token": token, "challenge": targets, "grid": grid}


# ---------- 会话 ----------
def new_session(email):
    sid = secrets.token_hex(24)
    SESSIONS[sid] = {"email": email, "exp": now_ts() + SESSION_TTL}
    return sid


def get_session_email(handler):
    cookie = handler.headers.get("Cookie", "")
    m = re.search(r"sid=([0-9a-f]+)", cookie)
    if not m:
        return None
    sid = m.group(1)
    info = SESSIONS.get(sid)
    if not info or info["exp"] < now_ts():
        SESSIONS.pop(sid, None)
        return None
    return info["email"]


def require_admin(handler):
    email = get_session_email(handler)
    if not email:
        return None, {"ok": False, "msg": "未登录"}, 401
    u = load_data()["users"].get(email)
    if not u or u.get("role") != "admin":
        return None, {"ok": False, "msg": "无管理员权限"}, 403
    return email, None, None


# ---------- 业务动作 ----------
def do_checkin(email):
    with _lock:
        data = load_data()
        u = data["users"].get(email)
        if not u:
            return {"ok": False, "msg": "请先登录"}
        if u.get("banned"):
            return {"ok": False, "msg": "账号已被封禁"}
        t = today_str()
        if u["last_checkin"] == t:
            return {"ok": False, "msg": "今天已经签到啦"}
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        u["checkin_streak"] = u["checkin_streak"] + 1 if u["last_checkin"] == y else 1
        reward = CHECKIN_BASE + (u["checkin_streak"] - 1) * CHECKIN_STREAK_BONUS
        u["points"] += reward
        u["last_checkin"] = t
        log_points(data, email, reward, f"每日签到（连续 {u['checkin_streak']} 天）")
        data["users"][email] = u
        save_data(data)
        return {"ok": True, "msg": f"签到成功，+{reward} 积分", "reward": reward}


def do_redeem(email, pid):
    with _lock:
        data = load_data()
        u = data["users"].get(email)
        if not u:
            return {"ok": False, "msg": "请先登录"}
        if u.get("banned"):
            return {"ok": False, "msg": "账号已被封禁"}
        prod = next((p for p in data["products"] if p["id"] == pid), None)
        if not prod:
            return {"ok": False, "msg": "商品不存在"}
        prod.setdefault("type", "normal")
        prod.setdefault("codes", [])
        if prod.get("stock", 0) <= 0 and prod["type"] != "card":
            return {"ok": False, "msg": "已兑完"}
        if prod["type"] == "card" and not prod["codes"]:
            return {"ok": False, "msg": "卡密已兑完"}
        if u["points"] < prod["cost"]:
            return {"ok": False, "msg": "积分不足"}
        u["points"] -= prod["cost"]
        rec = {
            "name": prod["name"], "icon": prod.get("icon", "gift"), "cost": prod["cost"],
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "code": None,
        }
        if prod["type"] == "card":
            rec["code"] = prod["codes"].pop(0)
            prod["stock"] = len(prod["codes"])
            rec["msg"] = f"兑换成功，卡密：{rec['code']}"
        else:
            prod["stock"] -= 1
        u["exchanges"].insert(0, rec)
        log_points(data, email, -prod["cost"], f"兑换 {prod['name']}")
        data["users"][email] = u
        save_data(data)
        return {"ok": True, "msg": rec.get("msg") or f"兑换成功：{prod['name']}"}


# ---------- 兑换码（货币 / 商品 两类奖励） ----------
def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去除易混字符 0/O/1/I


def gen_code_str():
    return "-".join("".join(random.choice(CODE_CHARS) for _ in range(4)) for _ in range(3))


def do_gen_codes(spec):
    """按 spec 批量生成兑换码。spec: kind / points / diamonds / emeralds / pid /
    count / max_uses / note / by。返回生成的码列表。"""
    with _lock:
        data = load_data()
        data.setdefault("redeem_codes", [])
        existing = {c["code"] for c in data["redeem_codes"]}
        made = []
        n = max(1, int(spec.get("count", 1) or 1))
        for _ in range(n):
            c = gen_code_str()
            while c in existing:
                c = gen_code_str()
            existing.add(c)
            data["redeem_codes"].append({
                "code": c, "kind": spec["kind"],
                "points": int(spec.get("points", 0) or 0),
                "diamonds": int(spec.get("diamonds", 0) or 0),
                "emeralds": int(spec.get("emeralds", 0) or 0),
                "pid": spec.get("pid"),
                "uses": 0, "max_uses": int(spec.get("max_uses", 1) or 1),
                "created_by": spec.get("by", ""),
                "created_at": now_str(),
                "note": spec.get("note", ""),
            })
            made.append(c)
        save_data(data)
        return made


def do_redeem_code(email, raw):
    """用户用兑换码换取 货币 或 商品。"""
    code = (raw or "").strip().upper()
    if not code:
        return {"ok": False, "msg": "请输入兑换码"}
    with _lock:
        data = load_data()
        u = data["users"].get(email)
        if not u:
            return {"ok": False, "msg": "请先登录"}
        if u.get("banned"):
            return {"ok": False, "msg": "账号已被封禁"}
        rec = next((c for c in data["redeem_codes"] if c["code"] == code), None)
        if not rec:
            return {"ok": False, "msg": "兑换码无效或不存在"}
        maxu = rec.get("max_uses", 1)
        if maxu != -1 and rec.get("uses", 0) >= maxu:
            return {"ok": False, "msg": "该兑换码已用完"}
        granted = []
        if rec["kind"] == "currency":
            if rec.get("points"):
                u["points"] += rec["points"]; granted.append(f"{rec['points']} 积分")
            if rec.get("diamonds"):
                u["diamonds"] += rec["diamonds"]; granted.append(f"{rec['diamonds']} 钻石")
            if rec.get("emeralds"):
                u["emeralds"] += rec["emeralds"]; granted.append(f"{rec['emeralds']} 绿宝石")
            u.setdefault("points_log", [])
            u["points_log"].insert(0, {
                "time": now_str(), "delta": rec.get("points", 0),
                "balance": u["points"], "note": f"兑换码获得 {'、'.join(granted)}",
            })
            if len(u["points_log"]) > 200:
                u["points_log"] = u["points_log"][:200]
        else:  # 商品奖励：免费发放（卡密自动下发一条）
            pid = rec.get("pid")
            prod = next((p for p in data["products"] if p["id"] == pid), None)
            if not prod:
                return {"ok": False, "msg": "兑换码对应商品已下架"}
            ex = {"name": prod["name"], "icon": prod.get("icon", "gift"), "cost": 0,
                  "time": now_str(), "code": None, "free": True}
            if prod.get("type") == "card" and prod.get("codes"):
                ex["code"] = prod["codes"].pop(0)
                prod["stock"] = len(prod["codes"])
            else:
                if (prod.get("stock", 0) or 0) <= 0:
                    return {"ok": False, "msg": "商品已无库存，无法发放"}
                prod["stock"] -= 1
            u["exchanges"].insert(0, ex)
            granted.append(f"商品：{prod['name']}")
        rec["uses"] = rec.get("uses", 0) + 1
        data["users"][email] = u
        save_data(data)
        return {"ok": True, "msg": f"兑换成功，获得 {'、'.join(granted)}", "granted": granted}


# ---------- HTTP 处理 ----------
class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send_file(os.path.join(PUBLIC_DIR, "index.html"), "text/html; charset=utf-8")
        elif route == "/api/captcha":
            self._send_json(make_captcha())
        elif route == "/api/state":
            email = get_session_email(self)
            if not email:
                self._send_json({"ok": False, "msg": "未登录"}, 401)
                return
            data = load_data()
            u = data["users"].get(email)
            if u.get("banned"):
                self._send_json({"ok": False, "msg": "账号已被封禁"}, 403)
                return
            t = today_str()
            prods = [{**p, "type": p.get("type", "normal"), "codes": None} for p in data["products"]]
            self._send_json({
                "ok": True, "email": email, "points": u["points"],
                "diamonds": u.get("diamonds", 0), "emeralds": u.get("emeralds", 0),
                "role": u.get("role", "user"),
                "last_checkin": u["last_checkin"],
                "checked_in_today": u["last_checkin"] == t,
                "checkin_streak": u["checkin_streak"],
                "exchanges": u["exchanges"],
                "history": u.get("points_log", []),
                "products": prods,
            })
        elif route == "/api/admin/products":
            _, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            data = load_data()
            self._send_json({"ok": True, "products": data["products"]})
        elif route == "/api/admin/users":
            _, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            data = load_data()
            users = []
            for e, u in data["users"].items():
                users.append({
                    "email": e, "role": u.get("role", "user"), "points": u["points"],
                    "created_at": u.get("created_at", "-"), "banned": u.get("banned", False),
                    "exchanges": len(u.get("exchanges", [])),
                })
            users.sort(key=lambda x: (x["role"] != "admin", x["email"]))
            self._send_json({"ok": True, "users": users})
        elif route == "/api/admin/stats":
            _, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            data = load_data()
            t = today_str()
            users = list(data["users"].values())
            total_users = len(users)
            total_points = sum(u["points"] for u in users)
            orders = []
            today_new = 0
            today_redeem = 0
            for e, u in data["users"].items():
                if u.get("created_at", "").startswith(t):
                    today_new += 1
                for ex in u.get("exchanges", []):
                    if ex.get("time", "").startswith(t):
                        today_redeem += 1
                    orders.append({"email": e, **ex})
            total_redeem = sum(len(u.get("exchanges", [])) for u in users)
            self._send_json({
                "ok": True,
                "stats": {
                    "total_users": total_users, "total_points": total_points,
                    "total_redeem": total_redeem, "today_new": today_new,
                    "today_redeem": today_redeem, "products": len(data["products"]),
                },
                "orders": orders[:100],
            })
        elif route.startswith("/static/"):
            rel = os.path.normpath(route[len("/static/"):])
            fp = os.path.join(PUBLIC_DIR, rel)
            if fp.startswith(PUBLIC_DIR):
                ct = "text/css; charset=utf-8" if rel.endswith(".css") else "application/javascript; charset=utf-8"
                self._send_file(fp, ct)
            else:
                self.send_error(403)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        if route == "/api/captcha/verify":
            token = payload.get("token")
            order = payload.get("order", [])
            target = CAPTCHAS.get(token)
            if not target or order != target:
                self._send_json({"ok": False, "msg": "验证失败，请重试"})
                return
            CAPTCHAS.pop(token, None)
            pt = secrets.token_hex(16)
            CAPTCHA_PASS[pt] = now_ts() + CAPTCHA_TTL
            self._send_json({"ok": True, "pass": pt})

        elif route == "/api/sendcode":
            email = (payload.get("email") or "").strip().lower()
            pt = payload.get("pass")
            if not is_email(email):
                self._send_json({"ok": False, "msg": "邮箱格式不正确"})
                return
            if not pt or CAPTCHA_PASS.get(pt, 0) < now_ts():
                self._send_json({"ok": False, "msg": "请先通过人机验证"})
                return
            rec = CODES.get(email)
            if rec and rec["last"] and now_ts() - rec["last"] < CODE_COOLDOWN:
                left = int(CODE_COOLDOWN - (now_ts() - rec["last"]))
                self._send_json({"ok": False, "msg": f"发送过于频繁，请 {left} 秒后再试", "next_in": left})
                return
            code = f"{random.randint(0, 999999):06d}"
            CODES[email] = {"code": code, "exp": now_ts() + CODE_TTL, "last": now_ts()}
            send_code_email(email, code)
            resp = {"ok": True, "msg": "验证码已发送，请查收邮箱", "next_in": CODE_COOLDOWN}
            if DEV_MODE:
                resp["dev_code"] = code
                resp["msg"] = "开发模式：验证码已回显（配置 SMTP 授权码后才会真发邮件）"
            self._send_json(resp)

        elif route == "/api/register":
            email = (payload.get("email") or "").strip().lower()
            pw = payload.get("password") or ""
            code = (payload.get("code") or "").strip()
            if not is_email(email):
                self._send_json({"ok": False, "msg": "邮箱格式不正确"})
                return
            if len(pw) < 6:
                self._send_json({"ok": False, "msg": "密码至少 6 位"})
                return
            rec = CODES.get(email)
            if not rec or rec["code"] != code or rec["exp"] < now_ts():
                self._send_json({"ok": False, "msg": "验证码错误或已过期"})
                return
            with _lock:
                data = load_data()
                if email in data["users"]:
                    self._send_json({"ok": False, "msg": "该邮箱已注册，请直接登录"})
                    return
                u = new_user()
                u["pw"] = hash_pw(pw)
                data["users"][email] = u
                save_data(data)
            CODES.pop(email, None)
            sid = new_session(email)
            self._send_json({"ok": True, "msg": "注册成功", "registered": True},
                            cookie=f"sid={sid}; Path=/; HttpOnly; Max-Age={SESSION_TTL}")

        elif route == "/api/login":
            email = (payload.get("email") or "").strip().lower()
            mode = payload.get("mode", "password")
            if not is_email(email):
                self._send_json({"ok": False, "msg": "邮箱格式不正确"})
                return
            data = load_data()
            u = data["users"].get(email)
            if u and u.get("banned"):
                self._send_json({"ok": False, "msg": "账号已被封禁"})
                return
            if mode == "password":
                pw = payload.get("password") or ""
                if not u or not u.get("pw") or not verify_pw(pw, u["pw"]):
                    self._send_json({"ok": False, "msg": "邮箱或密码错误"})
                    return
            else:
                code = (payload.get("code") or "").strip()
                rec = CODES.get(email)
                if not u:
                    self._send_json({"ok": False, "msg": "该邮箱未注册，请先注册"})
                    return
                if not rec or rec["code"] != code or rec["exp"] < now_ts():
                    self._send_json({"ok": False, "msg": "验证码错误或已过期"})
                    return
                CODES.pop(email, None)
            sid = new_session(email)
            self._send_json({"ok": True, "msg": "登录成功"},
                            cookie=f"sid={sid}; Path=/; HttpOnly; Max-Age={SESSION_TTL}")

        elif route == "/api/logout":
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"sid=([0-9a-f]+)", cookie)
            if m:
                SESSIONS.pop(m.group(1), None)
            self._send_json({"ok": True, "msg": "已退出登录"},
                            cookie="sid=; Path=/; HttpOnly; Max-Age=0")

        # ===== 管理员接口 =====
        elif route in ("/api/admin/products", "/api/admin/add_product"):
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            if route == "/api/admin/products":
                data = load_data()
                self._send_json({"ok": True, "products": data["products"]})
                return
            # add_product
            name = (payload.get("name") or "").strip()
            ptype = payload.get("type", "normal")
            try:
                cost = int(payload.get("cost", 0))
            except Exception:
                cost = 0
            if not name or cost <= 0:
                self._send_json({"ok": False, "msg": "请填写商品名称与有效积分价格"})
                return
            icon = (payload.get("icon") or "gift").strip() or "gift"
            desc = (payload.get("desc") or "").strip()
            with _lock:
                data = load_data()
                prod = {"id": "c" + secrets.token_hex(5), "name": name, "icon": icon,
                        "cost": cost, "desc": desc, "stock": 0, "type": ptype, "codes": []}
                if ptype == "card":
                    codes = [c.strip() for c in (payload.get("codes") or "").splitlines() if c.strip()]
                    if not codes:
                        self._send_json({"ok": False, "msg": "卡密商品请至少填写一条卡密"})
                        return
                    prod["codes"] = codes
                    prod["stock"] = len(codes)
                else:
                    try:
                        prod["stock"] = int(payload.get("stock", 0))
                    except Exception:
                        prod["stock"] = 0
                data["products"].append(prod)
                save_data(data)
            self._send_json({"ok": True, "msg": f"已上架：{name}"})

        elif route == "/api/admin/edit_product":
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            pid = payload.get("id")
            with _lock:
                data = load_data()
                prod = next((p for p in data["products"] if p["id"] == pid), None)
                if not prod:
                    self._send_json({"ok": False, "msg": "商品不存在"})
                    return
                if payload.get("name") is not None:
                    prod["name"] = str(payload["name"]).strip() or prod["name"]
                if payload.get("icon") is not None:
                    prod["icon"] = str(payload["icon"]).strip() or prod["icon"]
                if payload.get("desc") is not None:
                    prod["desc"] = str(payload["desc"]).strip()
                if payload.get("cost") is not None:
                    try:
                        c = int(payload["cost"])
                        if c > 0:
                            prod["cost"] = c
                    except Exception:
                        pass
                if prod.get("type") != "card" and payload.get("stock") is not None:
                    try:
                        prod["stock"] = max(0, int(payload["stock"]))
                    except Exception:
                        pass
                save_data(data)
            self._send_json({"ok": True, "msg": "已保存修改"})

        elif route == "/api/admin/delete_product":
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            pid = payload.get("id")
            with _lock:
                data = load_data()
                before = len(data["products"])
                data["products"] = [p for p in data["products"] if p["id"] != pid]
                if len(data["products"]) == before:
                    self._send_json({"ok": False, "msg": "商品不存在"})
                    return
                # 删的是默认商品 → 记墓碑，防止 load_data 兜底逻辑把它补回来
                if pid in DEFAULT_IDS:
                    dd = data.setdefault("deleted_defaults", [])
                    if pid not in dd:
                        dd.append(pid)
                save_data(data)
            self._send_json({"ok": True, "msg": "已删除商品"})

        elif route == "/api/admin/restore_defaults":
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            with _lock:
                data = load_data()
                data["deleted_defaults"] = []
                # 立刻补回全部默认商品（load_data 会因墓碑清空而自动补齐）
                ids = {p.get("id") for p in data["products"]}
                for dp in DEFAULT_PRODUCTS:
                    if dp["id"] not in ids:
                        data["products"].append(dict(dp, codes=[]))
                        ids.add(dp["id"])
                save_data(data)
            self._send_json({"ok": True, "msg": "已恢复全部默认商品"})

        elif route == "/api/admin/add_codes":
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            pid = payload.get("id")
            codes = [c.strip() for c in (payload.get("codes") or "").splitlines() if c.strip()]
            if not codes:
                self._send_json({"ok": False, "msg": "请填写要补充的卡密"})
                return
            with _lock:
                data = load_data()
                prod = next((p for p in data["products"] if p["id"] == pid), None)
                if not prod:
                    self._send_json({"ok": False, "msg": "商品不存在"})
                    return
                prod.setdefault("codes", [])
                prod["codes"].extend(codes)
                prod["stock"] = len(prod["codes"])
                save_data(data)
            self._send_json({"ok": True, "msg": f"已补充 {len(codes)} 条卡密"})

        # ===== 用户管理 =====
        elif route == "/api/admin/update_user":
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            target = (payload.get("email") or "").strip().lower()
            action = payload.get("action")
            with _lock:
                data = load_data()
                u = data["users"].get(target)
                if not u:
                    self._send_json({"ok": False, "msg": "用户不存在"})
                    return
                if action == "add" or action == "sub":
                    try:
                        amt = int(payload.get("amount", 0))
                    except Exception:
                        amt = 0
                    if amt <= 0:
                        self._send_json({"ok": False, "msg": "金额需大于 0"})
                        return
                    if action == "add":
                        u["points"] += amt
                        log_points(data, target, amt, "管理员发放积分")
                    else:
                        u["points"] = max(0, u["points"] - amt)
                        log_points(data, target, -amt, "管理员扣减积分")
                    self._send_json({"ok": True, "msg": f"已{'发放' if action=='add' else '扣减'} {amt} 积分"})
                elif action == "ban":
                    u["banned"] = True
                    self._send_json({"ok": True, "msg": "已封禁该用户"})
                elif action == "unban":
                    u["banned"] = False
                    self._send_json({"ok": True, "msg": "已解封该用户"})
                elif action == "delete":
                    if u.get("role") == "admin":
                        self._send_json({"ok": False, "msg": "不能删除管理员"})
                        return
                    del data["users"][target]
                    save_data(data)
                    self._send_json({"ok": True, "msg": "已删除用户"})
                    return
                else:
                    self._send_json({"ok": False, "msg": "未知操作"})
                    return
                data["users"][target] = u
                save_data(data)

        elif route == "/api/checkin":
            email = get_session_email(self)
            if not email:
                self._send_json({"ok": False, "msg": "未登录"}, 401)
                return
            self._send_json(do_checkin(email))
        elif route == "/api/redeem":
            email = get_session_email(self)
            if not email:
                self._send_json({"ok": False, "msg": "未登录"}, 401)
                return
            self._send_json(do_redeem(email, payload.get("id")))
        elif route == "/api/redeem_code":
            email = get_session_email(self)
            if not email:
                self._send_json({"ok": False, "msg": "未登录"}, 401)
                return
            self._send_json(do_redeem_code(email, payload.get("code")))

        # ===== 兑换码管理 =====
        elif route in ("/api/admin/codes", "/api/admin/gen_code", "/api/admin/delete_code"):
            email, err, code = require_admin(self)
            if err:
                self._send_json(err, code); return
            if route == "/api/admin/codes":
                data = load_data()
                self._send_json({"ok": True, "codes": data.get("redeem_codes", [])})
                return
            if route == "/api/admin/gen_code":
                kind = payload.get("kind", "currency")
                if kind == "currency":
                    try:
                        pts = int(payload.get("points", 0) or 0)
                        dia = int(payload.get("diamonds", 0) or 0)
                        eme = int(payload.get("emeralds", 0) or 0)
                    except Exception:
                        pts = dia = eme = 0
                    if pts <= 0 and dia <= 0 and eme <= 0:
                        self._send_json({"ok": False, "msg": "请至少填写一种货币数量"})
                        return
                    made = do_gen_codes({"kind": "currency", "points": pts, "diamonds": dia,
                                         "emeralds": eme, "count": payload.get("count", 1),
                                         "max_uses": payload.get("max_uses", 1),
                                         "note": payload.get("note", ""), "by": email})
                    self._send_json({"ok": True, "msg": f"已生成 {len(made)} 个兑换码",
                                     "codes": made})
                else:
                    pid = payload.get("pid")
                    data = load_data()
                    prod = next((p for p in data["products"] if p["id"] == pid), None)
                    if not prod:
                        self._send_json({"ok": False, "msg": "请选择有效商品"})
                        return
                    made = do_gen_codes({"kind": "product", "pid": pid,
                                         "count": payload.get("count", 1),
                                         "max_uses": payload.get("max_uses", 1),
                                         "note": payload.get("note", ""), "by": email})
                    self._send_json({"ok": True, "msg": f"已生成 {len(made)} 个商品兑换码",
                                     "codes": made})
                return
            # delete_code
            cd = (payload.get("code") or "").strip().upper()
            with _lock:
                data = load_data()
                before = len(data.get("redeem_codes", []))
                data["redeem_codes"] = [c for c in data.get("redeem_codes", [])
                                        if c["code"] != cd]
                if len(data["redeem_codes"]) == before:
                    self._send_json({"ok": False, "msg": "兑换码不存在"})
                    return
                save_data(data)
            self._send_json({"ok": True, "msg": "已删除兑换码"})
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def main():
    ensure_admin()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    mode = "开发模式(回显验证码)" if DEV_MODE else f"SMTP({EMAIL_USER})"
    print(f"积分商城已启动：http://localhost:{PORT}  [{mode}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
