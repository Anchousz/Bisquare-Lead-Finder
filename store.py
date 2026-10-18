"""Хранилище лидов: дедуп, флаги управления (избранное/удалено/проверено),
сортировка по собираемым полям, экспорт JSON/CSV. Потокобезопасно.
Персистентность — один JSON-файл, атомарная запись.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
from datetime import datetime

_MGMT = ("favorite", "deleted", "checked")

SORT_KEYS = {
    "score": "score",
    "name": "name",
    "city": "city",
    "rating": "rating",
    "reviews": "reviews_count",
    "date": "scraped_at",
}

# «Показать»: способ связи или статус звонка. Отказавшиеся лежат в корзине.
_HAS = {
    "has_phone":      lambda l: bool(str(l.get("phone") or "").strip()),
    "has_site":       lambda l: bool(str(l.get("website") or "").strip()),
    "no_site":        lambda l: not str(l.get("website") or "").strip(),
    "has_socials":    lambda l: bool(l.get("socials")),
    "has_messengers": lambda l: bool(l.get("messengers")),
    "has_email":      lambda l: bool(str(l.get("email") or "").strip()),
    "call_new":       lambda l: (l.get("call_status") or "new") == "new",
    "call_progress":  lambda l: l.get("call_status") == "in_progress",
    "call_deal":      lambda l: l.get("call_status") in ("deal", "interested"),
    "call_back":      lambda l: l.get("call_status") in ("callback", "no_answer"),
}


def _empty(v):
    return v is None or v == "" or v == [] or v == {}


def _norm_site(l):
    """PHP отдаёт пустой объект сайта как [] — приводим к {}, иначе
    site.get(...) падает, а в окне [] считается «данными»."""
    if not isinstance(l.get("site"), dict):
        l["site"] = {}
    return l

# порядок колонок CSV / порядок полей в JSON-карточке (сортировка по пунктам сбора)
FIELD_ORDER = [
    "id", "score", "status", "name", "industry", "city", "address",
    "phone", "email", "website", "socials", "messengers",
    "rating", "reviews_count", "hours", "social_activity",
    "site_reachable", "site_ssl", "site_mobile", "site_speed_ms", "site_pagespeed",
    "site_cms", "site_has_form", "site_has_booking", "site_seo", "site_cta",
    "recommendation", "reasons", "favorite", "checked", "source", "scraped_at",
]


def _norm_domain(url):
    if not url:
        return ""
    u = url.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    try:
        import urllib.parse
        return urllib.parse.urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _digits(s):
    return re.sub(r"\D", "", s or "")


class LeadStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._leads = {}          # id -> lead
        # Что изменилось в этом окне и ещё не ушло на сервер. Отправляем только
        # это, а не всю базу: иначе старая копия затирала бы чужие свежие данные.
        self._dirty = set()
        self._load()

    # ---------- персистентность ----------
    def _load(self):
        # path=None — работа без диска: база живёт на сервере, а здесь только
        # кеш в памяти. Так на машине менеджера не остаётся файла с клиентами.
        if not self.path:
            return
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for l in (data.get("leads") if isinstance(data, dict) else data) or []:
                    if l.get("id"):
                        self._leads[l["id"]] = l
        except Exception:
            self._leads = {}

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": datetime.now().isoformat(timespec="seconds"),
                       "leads": list(self._leads.values())}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---------- ключ дедупа ----------
    @staticmethod
    def make_id(lead):
        key = "|".join([
            _digits(lead.get("phone")),
            _norm_domain(lead.get("website")),
            (lead.get("name") or "").strip().lower(),
            (lead.get("city") or "").strip().lower(),
        ])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    # ---------- запись ----------
    def upsert_one(self, lead):
        with self._lock:
            lid = self.make_id(lead)
            lead["id"] = lid
            prev = self._leads.get(lid)
            self._dirty.add(lid)
            if prev:
                # Флаги управления — прежние. Собранное обновляем, но пустое новое
                # значение не затирает заполненное старое: повторная находка без
                # проверки сайта не должна стереть уже проверенный сайт или рейтинг.
                merged = dict(prev)
                for k, v in lead.items():
                    if k in _MGMT:
                        continue
                    if _empty(v) and not _empty(prev.get(k)):
                        continue
                    merged[k] = v
                self._leads[lid] = _norm_site(merged)
                self._save()
                return self._leads[lid], False
            for k in _MGMT:
                lead.setdefault(k, False)
            _norm_site(lead)
            self._leads[lid] = lead
            self._save()
            return lead, True

    def load_many(self, leads):
        """Положить лиды, пришедшие с сервера, как есть — со статусом звонка и
        отметками. id не пересчитываем: он тот же, что на сервере."""
        n = 0
        with self._lock:
            for l in leads or []:
                lid = l.get("id")
                if not lid:
                    continue
                for k in _MGMT:
                    l[k] = bool(int(l.get(k) or 0)) if not isinstance(l.get(k), bool) else l[k]
                self._leads[lid] = _norm_site({**self._leads.get(lid, {}), **l})
                n += 1
        return n

    def mark_dirty(self, lid):
        with self._lock:
            if lid in self._leads:
                self._dirty.add(lid)

    def dirty(self):
        """Лиды, изменённые в этом окне и ещё не отправленные."""
        with self._lock:
            return [self._leads[i] for i in self._dirty if i in self._leads]

    def clear_dirty(self, ids):
        with self._lock:
            self._dirty.difference_update(ids)

    def upsert_many(self, leads):
        added = 0
        for l in leads:
            _, is_new = self.upsert_one(l)
            added += 1 if is_new else 0
        return added

    def set_flag(self, lid, action):
        with self._lock:
            l = self._leads.get(lid)
            if not l:
                return False
            act = {
                "fav": ("favorite", True), "unfav": ("favorite", False),
                "check": ("checked", True), "uncheck": ("checked", False),
                "delete": ("deleted", True), "restore": ("deleted", False),
            }.get(action)
            if not act:
                return False
            l[act[0]] = act[1]
            self._save()
            return True

    def purge_deleted(self):
        with self._lock:
            gone = [i for i, l in self._leads.items() if l.get("deleted")]
            for i in gone:
                del self._leads[i]
            self._save()
            return len(gone)

    def clear_all(self):
        with self._lock:
            n = len(self._leads)
            self._leads = {}
            self._save()
            return n

    # ---------- чтение ----------
    def all(self):
        with self._lock:
            return list(self._leads.values())

    def _view(self, leads, view):
        if view == "deleted":
            return [l for l in leads if l.get("deleted")]
        leads = [l for l in leads if not l.get("deleted")]
        if view == "fav":
            return [l for l in leads if l.get("favorite")]
        if view == "unchecked":
            return [l for l in leads if not l.get("checked")]
        return leads  # all

    def list(self, view="all", sort="score", direction="desc", q="", status="", show="", min_score=0):
        with self._lock:
            leads = self._view(list(self._leads.values()), view)
        if q:
            ql = q.lower()
            leads = [l for l in leads if ql in " ".join(str(l.get(k) or "") for k in
                     ("name", "city", "industry", "address", "phone", "website")).lower()]
        if status == "has_phone":          # старое значение фильтра — теперь это «Показать»
            show, status = show or "has_phone", ""
        if status:
            leads = [l for l in leads if l.get("status") == status]
        if show in _HAS:
            leads = [l for l in leads if _HAS[show](l)]
        if min_score:
            leads = [l for l in leads if int(l.get("score") or 0) >= int(min_score)]
        field = SORT_KEYS.get(sort, "score")
        desc = direction != "asc"

        # Пустые значения — всегда в конце, в любом направлении; при равенстве —
        # по скору и имени, иначе сотни одинаковых дат выглядят как «не сортирует».
        def key(l):
            v = l.get(field)
            return v.strip().lower() if isinstance(v, str) else v

        filled = [l for l in leads if not _empty(key(l))]
        blank = [l for l in leads if _empty(key(l))]
        tie = lambda l: (-(l.get("score") or 0), str(l.get("name") or "").lower())
        filled.sort(key=tie)
        blank.sort(key=tie)
        try:
            filled.sort(key=key, reverse=desc)
        except TypeError:                  # смешанные типы (число и строка) — сравниваем как текст
            filled.sort(key=lambda l: str(key(l)), reverse=desc)
        return filled + blank

    def stats(self):
        with self._lock:
            leads = list(self._leads.values())
        active = [l for l in leads if not l.get("deleted")]
        return {
            "total": len(active),
            "hot": sum(1 for l in active if l.get("status") == "hot"),
            "warm": sum(1 for l in active if l.get("status") == "warm"),
            "cold": sum(1 for l in active if l.get("status") == "cold"),
            "with_phone": sum(1 for l in active if str(l.get("phone") or "").strip()),
            # есть ли по чему сортировать: у OSM рейтинга и отзывов нет вовсе
            "unchecked_sites": sum(1 for l in active if str(l.get("website") or "").strip() and not (
                isinstance(l.get("site"), dict) and (l["site"].get("_done") or l["site"].get("reachable") is not None))),
            # сколько лидов под каждым пунктом «Показать» — видно, что фильтр
            # работает, а записей просто нет (у OSM нет мессенджеров и почты)
            "show_counts": {k: sum(1 for l in active if f(l)) for k, f in _HAS.items()},
            "with_rating": sum(1 for l in active if not _empty(l.get("rating"))),
            "with_reviews": sum(1 for l in active if not _empty(l.get("reviews_count"))),
            "favorites": sum(1 for l in active if l.get("favorite")),
            "unchecked": sum(1 for l in active if not l.get("checked")),
            "deleted": sum(1 for l in leads if l.get("deleted")),
        }

    # ---------- экспорт ----------
    @staticmethod
    def _flat(l):
        site = l.get("site") or {}
        row = {
            "id": l.get("id"), "score": l.get("score"), "status": l.get("status"),
            "name": l.get("name"), "industry": l.get("industry"), "city": l.get("city"),
            "address": l.get("address"), "phone": l.get("phone"), "email": l.get("email"),
            "website": l.get("website"),
            "socials": " ; ".join(l.get("socials") or []),
            "messengers": " ; ".join(l.get("messengers") or []),
            "rating": l.get("rating"), "reviews_count": l.get("reviews_count"),
            "hours": l.get("hours"), "social_activity": l.get("social_activity"),
            "site_reachable": site.get("reachable"), "site_ssl": site.get("ssl"),
            "site_mobile": site.get("mobile"), "site_speed_ms": site.get("speed_ms"),
            "site_pagespeed": site.get("pagespeed"), "site_cms": site.get("cms"),
            "site_has_form": site.get("has_form"), "site_has_booking": site.get("has_booking"),
            "site_seo": site.get("seo"), "site_cta": site.get("cta"),
            "recommendation": l.get("recommendation"),
            "reasons": " ; ".join(l.get("reasons") or []),
            "favorite": bool(l.get("favorite")), "checked": bool(l.get("checked")),
            "source": l.get("source"), "scraped_at": l.get("scraped_at"),
        }
        # строгий порядок полей = «сортировка json по пунктам, которые собираются»
        return {k: row.get(k) for k in FIELD_ORDER}

    def export_bytes(self, fmt="json", view="all", sort="score", direction="desc", q="", status="",
                     show="", min_score=0):
        leads = self.list(view=view, sort=sort, direction=direction, q=q, status=status,
                          show=show, min_score=min_score)
        rows = [self._flat(l) for l in leads]
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=FIELD_ORDER, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
            data = ("﻿" + buf.getvalue()).encode("utf-8")
            ct = "text/csv; charset=utf-8"
        else:
            data = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            ct = "application/json; charset=utf-8"
        day = datetime.now().strftime("%Y%m%d_%H%M")
        fname = f"bisquare_leads_{view}_{day}.{fmt}"
        return data, ct, fname, len(rows)

    def export_to_dir(self, out_dir, fmt="json", **kw):
        data, ct, fname, n = self.export_bytes(fmt=fmt, **kw)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, fname)
        with open(path, "wb") as f:
            f.write(data)
        return path, n
