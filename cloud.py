"""Связь приложения с сервером Bisquare.

Через этот модуль проходит всё, что раньше жило на машине менеджера:
  · вход и разрешение работать (рубильник на стороне сервера);
  · запросы к 2ГИС/Яндексу/ВК — ключ подставляет сервер, сюда он не попадает;
  · общая база лидов и стоп-лист.

На диске остаётся ровно одна вещь — токен устройства: он не данные клиентов,
а пропуск, и при отзыве доступа стирается вместе с приложением.

Сеть здесь принципиально «мягкая»: сервер недоступен — приложение не падает,
а сообщает, что работать нельзя. Молча продолжать поиск без разрешения нельзя.
"""
from __future__ import annotations

import json
import os
import platform
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_SSL_CTX = ssl.create_default_context()

APP_NAME = "BisquareLeadFinder"
# Версию сборки передаёт Electron (из package.json); сервер по ней может не
# пустить к поиску устаревшее приложение — «обнови приложение».
APP_VERSION = os.environ.get("BISQUARE_APP_VERSION") or "2.1.1"

# Адрес сервера задаётся переменной окружения BISQUARE_SERVER
# (в приватной сборке подставляется установщиком/лаунчером).
SERVER_URL = os.environ.get("BISQUARE_SERVER", "").rstrip("/")

# Источники, которые умеет проксировать сервер. Остальные (OSM/Overpass/Nominatim)
# ключей не требуют и ходят напрямую — гонять их через сервер незачем.
PROXIED = {
    "2gis":      "3.0/items",
    "yandex":    "v1/",
    "vk":        "method/groups.search",
    "pagespeed": "pagespeedonline/v5/runPagespeed",
}


def machine_id() -> str:
    """Отпечаток машины. Нужен, чтобы скопированный токен не заработал на другом
    компьютере: сервер сверяет отпечаток и гасит устройство при расхождении."""
    try:
        raw = f"{platform.node()}|{uuid.getnode()}|{platform.machine()}"
    except Exception:
        raw = "unknown"
    import hashlib
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:32]


def machine_name() -> str:
    try:
        return platform.node() or "ПК"
    except Exception:
        return "ПК"


class CloudError(Exception):
    """Сервер ответил отказом. Текст — уже человеческий, его можно показать."""

    def __init__(self, msg, code="", status=0):
        super().__init__(msg)
        self.code = code
        self.status = status


class Cloud:
    def __init__(self, data_dir, log=None):
        self.data_dir = data_dir
        self.log = log or (lambda m: None)
        self._lock = threading.RLock()

        self.base_url = SERVER_URL
        self.token = ""
        self.user = {}
        # Последний ответ heartbeat: что сервер разрешает прямо сейчас.
        self.state = {
            "ok": False,
            "access": "unknown",
            "note": "",
            "search_allowed": False,
            "search_reason": "нет связи с сервером",
            "limits": {},
            "server_sources": {},
            "keys_mode": "own_or_server",
            "allow_own_keys": True,
        }
        self.last_beat = 0.0
        self.wipe_requested = False
        self.wipe_installer = False
        self._load_token()

    # ------------------------------------------------------------------ файл
    @property
    def _token_path(self):
        return os.path.join(self.data_dir, "device.json")

    def _load_token(self):
        try:
            with open(self._token_path, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
            self.token = str(d.get("token") or "")
            self.user = d.get("user") or {}
            # Если адрес в коде сменили (новая сборка) — он важнее записанного
            # в старом файле пропуска, иначе приложение стучалось бы по старому.
            self.base_url = SERVER_URL or str(d.get("server") or "")
        except Exception:
            self.token, self.user = "", {}
            self.base_url = SERVER_URL

    def _save_token(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self._token_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"token": self.token, "user": self.user,
                           "server": self.base_url}, f, ensure_ascii=False)
            os.replace(tmp, self._token_path)
        except Exception as e:
            self.log(f"[cloud] не удалось сохранить вход: {e}")

    def forget(self):
        """Выход: забываем пропуск. Данные и так лежат на сервере."""
        with self._lock:
            self.token, self.user = "", {}
            self.state.update({"ok": False, "access": "unknown",
                               "search_allowed": False,
                               "search_reason": "нужен вход"})
        try:
            os.remove(self._token_path)
        except Exception:
            pass

    # ------------------------------------------------------------------ сеть
    def _url(self, path):
        base = (self.base_url or "").rstrip("/")
        if not base:
            raise CloudError("не указан адрес сервера", "no_server")
        return f"{base}/api/{path}"

    def _request(self, path, payload=None, method="POST", timeout=25, auth=True):
        url = self._url(path)
        data = None
        headers = {
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            "Accept": "application/json",
            "X-Bisquare-Version": APP_VERSION,
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise CloudError("нужен вход", "no_token")
            headers["X-Bisquare-Device"] = self.token
            headers["X-Bisquare-Machine"] = machine_id()

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                body = r.read().decode("utf-8", "replace")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                d = json.loads(raw) if raw else {}
            except Exception:
                d = {}
            msg = d.get("msg") or d.get("error") or f"сервер ответил {e.code}"
            raise CloudError(msg, d.get("error", ""), e.code)
        except urllib.error.URLError as e:
            raise CloudError(f"нет связи с сервером: {e.reason}", "offline")
        except Exception as e:
            raise CloudError(f"сбой связи: {e}", "failed")

    # ------------------------------------------------------------------ вход
    def login(self, server, login, password):
        # Адрес приходит из кода (SERVER_URL). Параметр оставлен на случай
        # нестандартной сборки, но обычно окно его не передаёт.
        server = (server or "").strip().rstrip("/") or SERVER_URL
        if not server:
            raise CloudError("не указан адрес сервера (переменная BISQUARE_SERVER)", "no_server")
        if not server.startswith(("http://", "https://")):
            server = "https://" + server
        self.base_url = server

        d = self._request("login.php", {
            "login": login,
            "password": password,
            "machine_id": machine_id(),
            "machine_name": machine_name(),
            "app_version": APP_VERSION,
        }, auth=False)

        if not d.get("ok"):
            raise CloudError(d.get("msg") or "не удалось войти", d.get("error", ""))

        with self._lock:
            self.token = str(d.get("device_token") or "")
            self.user = d.get("user") or {}
        self._save_token()
        self.log(f"[cloud] вход выполнен: {self.user.get('name') or self.user.get('login')}")
        self.beat()
        return self.user

    @property
    def logged_in(self) -> bool:
        return bool(self.token and self.base_url)

    # -------------------------------------------------------------- heartbeat
    def beat(self):
        """Спросить сервер, можно ли работать. Вызывается при старте и по таймеру."""
        if not self.logged_in:
            with self._lock:
                self.state.update({"ok": False, "access": "unknown",
                                   "search_allowed": False, "search_reason": "нужен вход"})
            return self.state
        try:
            d = self._request("session.php", {})
        except CloudError as e:
            # Связи нет — работать нельзя, но приложение живёт: человек увидит причину.
            with self._lock:
                self.state.update({"ok": False, "search_allowed": False,
                                   "search_reason": str(e)})
                if e.code in ("bad_device_token", "device_mismatch", "device_revoked"):
                    self.state["access"] = "revoked"
                    self.state["search_reason"] = str(e)
            return self.state

        with self._lock:
            acc = (d.get("access") or {})
            self.state = {
                "ok": True,
                "access": acc.get("access", "active"),
                "note": acc.get("note", ""),
                "is_owner": bool(acc.get("is_owner")),
                "can_export": bool(acc.get("can_export")),
                "can_book": bool(acc.get("can_book")),
                "can_finance": bool(acc.get("can_finance")),
                "search_allowed": bool(d.get("search_allowed")),
                "search_reason": d.get("search_reason", ""),
                "limits": d.get("limits") or {},
                "server_sources": d.get("server_sources") or {},
                "keys_mode": d.get("keys_mode", "own_or_server"),
                # Сервер требует версию новее — окно покажет «обнови приложение».
                "update_required": bool(d.get("update_required")),
                "min_app_version": d.get("min_app_version", ""),
                "app_version": APP_VERSION,
                "allow_own_keys": bool(d.get("allow_own_keys", True)),
                "stoplist": d.get("stoplist") or {},
                "user": d.get("user") or self.user,
            }
            self.last_beat = time.time()
            if d.get("wipe"):
                self.wipe_requested = True
                self.wipe_installer = bool(d.get("wipe_installer"))
        return self.state

    def start_heartbeat(self, interval=30, on_wipe=None):
        """Фоновая проверка разрешения. Отдельный поток, демон — не держит выход."""
        def loop():
            while True:
                try:
                    self.beat()
                    if self.wipe_requested and on_wipe:
                        on_wipe(self.wipe_installer)
                        return
                except Exception:
                    pass
                time.sleep(interval)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------- источники
    def proxy(self, source, endpoint, params, own_key="", timeout=40):
        """Запрос к внешнему источнику руками сервера.

        Серверный ключ остаётся на сервере — сюда приходит только ответ
        источника. Свой ключ менеджера (он вписан в его же настройках) уходит
        этим запросом, если владелец разрешил работать на своих ключах.

        Отказ самого сервера (нет ключа, квота, стоп) — исключение. Ошибка
        источника с телом (2ГИС meta.code, ошибка PageSpeed) возвращается как
        есть: по ней видно, заблокирован ли ключ или просто не открылся сайт.
        """
        payload = {"source": source, "endpoint": endpoint, "params": params or {}}
        if own_key:
            payload["own_key"] = own_key
        d = self._request("proxy.php", payload, timeout=timeout)
        data = d.get("data")
        if isinstance(data, dict):
            return data
        if not d.get("ok"):
            raise CloudError(d.get("msg") or f"источник ответил {d.get('status') or 'ошибкой'}",
                             d.get("error", ""), int(d.get("status") or 0))
        return {}

    # ------------------------------------------------------------------ база
    def push(self, leads):
        """Отправить собранное. Партиями — сервер не принимает больше 500 за раз."""
        if not leads:
            return {"added": 0, "updated": 0, "blocked": 0}
        total = {"added": 0, "updated": 0, "skipped": 0, "blocked": 0}
        for i in range(0, len(leads), 200):
            chunk = leads[i:i + 200]
            d = self._request("sync.php", {"action": "push", "leads": chunk}, timeout=60)
            for k in total:
                total[k] += int(d.get(k) or 0)
        return total

    def leads(self, **kw):
        q = urllib.parse.urlencode({k: v for k, v in kw.items() if v not in (None, "")})
        return self._request(f"leads.php?{q}", None, method="GET")

    def contact(self, lead_id, action, comment="", next_at=""):
        payload = {"lead_id": lead_id, "action": action}
        if comment:
            payload["comment"] = comment
        if next_at:
            payload["next_at"] = next_at
        return self._request("contact.php", payload)

    def stoplist(self):
        return self._request("stoplist.php", None, method="GET")

    def flag(self, lead_id, action):
        """Избранное / проверен / корзина — на сервер, иначе после перезапуска
        отметки бы пропали: база в приложении теперь только в памяти."""
        return self._request("sync.php", {"action": "flag", "lead_id": lead_id, "flag": action})

    def pull(self, max_pages=50, view="all"):
        """Забрать общую базу с сервера. Сервер сам решает, что видно этому
        человеку: без права «видит всех» — только его лиды."""
        out = []
        page = 1
        while page <= max_pages:
            d = self.leads(view=view, per_page=200, page=page, sort="score", dir="desc")
            rows = d.get("leads") or []
            out.extend(rows)
            if page >= int(d.get("pages") or 1) or not rows:
                break
            page += 1
        return out
