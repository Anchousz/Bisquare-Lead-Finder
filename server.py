"""Bisquare Lead Finder — локальный сервер + API + отдача интерфейса.

Запуск:  python server.py           (откроется http://127.0.0.1:8770)
         python server.py --port 9000 --no-open

Зависимостей для интерфейса/сбора не нужно (stdlib + urllib). Playwright требуется
только для анализа сайтов (галка «Анализировать сайты»); без него сбор всё равно работает.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cloud as cloud_mod
import leadfinder
import store as store_mod

# Windows-консоль по умолчанию cp1251 — принудительно UTF-8, чтобы print() не падал на «→»/кириллице
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")

# В установленном приложении код лежит в read-only папке (Program Files), поэтому база и
# настройки живут в пользовательской папке — её путь передаёт Electron через BISQUARE_DATA_DIR.
_USER_DATA = os.environ.get("BISQUARE_DATA_DIR")
DATA_DIR = _USER_DATA or os.path.join(APP_DIR, "leads_out")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json") if _USER_DATA else os.path.join(APP_DIR, "config.json")
STORE_PATH = os.path.join(DATA_DIR, "store.json")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

DEFAULT_CONFIG = {
    "cities": "Казань",
    "rubrics_filter": "",
    "exclude": "",
    "sources": ["2gis", "osm"],
    "gis_api_key": "",
    "yandex_api_key": "",
    "vk_token": "",
    "max_leads": 2000,
    "max_pages": 10,
    "api_budget": 800,
    "min_score": 40,
    "analyze_sites": True,
    "concurrency": 3,
    "headless": True,
    "pagespeed_key": "",
    "proxies": "",
    "out_dir": DATA_DIR,
    "cleanup_on_finish": False,
    # Адрес сервера зашит в коде (cloud.SERVER_URL) — менеджер его не вводит
    # и на экране входа не видит. Здесь только для справки в настройках.
    "server_url": cloud_mod.SERVER_URL,
}


# --------------------------------------------------------------------------
#  Состояние приложения
# --------------------------------------------------------------------------
class App:
    def __init__(self):
        # База лидов живёт на сервере. Локально — только кеш в памяти, чтобы
        # после закрытия приложения на машине менеджера не оставалось данных.
        self.store = store_mod.LeadStore(None)
        self.config = self._load_config()
        self.cloud = cloud_mod.Cloud(DATA_DIR, log=lambda m: self.log(m))
        self.cloud.base_url = self.cloud.base_url or str(self.config.get("server_url") or "")
        # Все источники с ключом (2ГИС, Яндекс, ВК, PageSpeed) после входа идут
        # через сервер — он подставляет ключ из панели. Без этой строки сбор
        # ходил бы напрямую и видел только ключи, вписанные в самом приложении.
        leadfinder.CLOUD = self.cloud
        # Команда на самоуничтожение приходит в heartbeat; её исполняет Electron.
        self.wipe = None
        self.log_lines = deque(maxlen=500)
        self.log_seq = 0
        self.running = False
        self.run_status = "idle"      # idle | running | error — для анимации кнопки
        # Итог прогона для окна: что закончилось (квота, ключ). seq растёт, чтобы
        # окно показало сообщение один раз.
        self.notice = {"seq": 0, "text": ""}
        # Выгрузка в файл — только с согласия владельца. Параметры запроса
        # запоминаем здесь: по разрешению скачивается ровно то, что просили.
        self.export_reqs = {}     # id запроса -> параметры списка
        self.export_keys = {}     # одноразовый ключ скачивания -> параметры списка
        self.added = 0
        self.stop_event = threading.Event()
        self.thread = None
        self._log_lock = threading.Lock()

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f) or {})
        except Exception:
            pass
        return cfg

    def save_config(self, patch):
        self.config.update(patch or {})
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def log(self, msg):
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        with self._log_lock:
            self.log_lines.append(line)
            self.log_seq += 1
        try:
            print("[bisquare]", msg, flush=True)
        except Exception:
            try:
                sys.stdout.buffer.write(("[bisquare] " + msg + "\n").encode("utf-8", "replace"))
                sys.stdout.flush()
            except Exception:
                pass

    def log_tail(self, n=250):
        with self._log_lock:
            return list(self.log_lines)[-n:], self.log_seq

    def clear_log(self):
        with self._log_lock:
            n = len(self.log_lines)
            self.log_lines.clear()
            self.log_seq += 1          # чтобы клиент заметил изменение и перерисовал
        return n

    # ---------- запуск сбора ----------
    def _allowed(self):
        """Можно ли сейчас работать. Разрешение даёт сервер — спрашиваем прямо
        сейчас, а не полагаемся на последний ответ: доступ могли отозвать минуту назад."""
        if self.running:
            return False, "уже выполняется", {}
        if not self.cloud.logged_in:
            return False, "нужен вход", {}
        st = self.beat()
        if st.get("access") == "revoked":
            return False, "доступ отозван", st
        if not st.get("search_allowed"):
            return False, st.get("search_reason") or "поиск сейчас недоступен", st
        return True, "", st

    _EXPORT_ARGS = ("view", "sort", "dir", "q", "status", "show", "min")
    _VIEW_WORD = {"fav": "избранные", "unchecked": "не проверено", "deleted": "корзина"}
    _SHOW_WORD = {"has_phone": "есть номер", "has_site": "есть сайт", "no_site": "нет сайта",
                  "has_socials": "есть соцсети", "has_messengers": "есть мессенджер", "has_email": "есть email",
                  "call_new": "не звонили", "call_progress": "в работе", "call_back": "перезвонить / недозвон",
                  "call_deal": "согласились"}

    def export_rows(self, a):
        return self.store.list(view=a.get("view") or "all", sort=a.get("sort") or "score",
                               direction=a.get("dir") or "desc", q=a.get("q") or "",
                               status=a.get("status") or "", show=a.get("show") or "",
                               min_score=int(a.get("min") or 0))

    def export_describe(self, a):
        """Какой список просят выгрузить — человеческими словами, для владельца."""
        parts = []
        if a.get("view") in self._VIEW_WORD:
            parts.append("вкладка «" + self._VIEW_WORD[a["view"]] + "»")
        if a.get("status"):
            parts.append(str(a["status"]))
        if a.get("show") in self._SHOW_WORD:
            parts.append(self._SHOW_WORD[a["show"]])
        if int(a.get("min") or 0):
            parts.append(f"скор от {int(a['min'])}")
        if a.get("q"):
            parts.append(f"поиск «{str(a['q'])[:40]}»")
        return " · ".join(parts) or "весь список"

    def export_request(self, body):
        a = {k: str(body.get(k) or "") for k in self._EXPORT_ARGS}
        a["format"] = "csv" if body.get("format") == "csv" else "json"
        n = len(self.export_rows(a))
        if not n:
            return {"ok": False, "msg": "в выбранном списке нет лидов — выгружать нечего"}
        if not self.cloud.logged_in:
            # Без входа база только локальная (демо) — спрашивать некого.
            rid = "local-" + secrets.token_hex(6)
            self.export_reqs[rid] = a
            return {"ok": True, "id": rid, "state": "approved"}
        try:
            d = self.cloud._request("export.php", {"action": "request", "fmt": a["format"], "rows": n,
                                                   "filters": self.export_describe(a)})
        except cloud_mod.CloudError as e:
            return {"ok": False, "msg": str(e)}
        rid = str(d.get("id"))
        self.export_reqs[rid] = a
        self.log(f"экспорт: запрос на выгрузку {n} лидов отправлен руководителю" if d.get("state") == "pending"
                 else f"экспорт: {n} лидов")
        return {"ok": True, "id": rid, "state": d.get("state"), "ttl_min": d.get("ttl_min")}

    def export_status(self, rid):
        if rid.startswith("local-"):
            return {"ok": True, "state": "approved"}
        try:
            d = self.cloud._request("export.php", {"action": "status", "id": int(rid)})
            return {"ok": True, "state": d.get("state")}
        except (cloud_mod.CloudError, ValueError) as e:
            return {"ok": False, "msg": str(e)}

    def export_claim(self, rid):
        """Израсходовать согласие и выдать окну одноразовый ключ на скачивание."""
        a = self.export_reqs.get(rid)
        if not a:
            return {"ok": False, "msg": "запрос не найден — запроси выгрузку снова"}
        if not rid.startswith("local-"):
            try:
                self.cloud._request("export.php", {"action": "use", "id": int(rid)})
            except (cloud_mod.CloudError, ValueError) as e:
                return {"ok": False, "msg": str(e)}
        self.export_reqs.pop(rid, None)
        key = secrets.token_hex(16)
        self.export_keys[key] = a
        return {"ok": True, "key": key}

    def _finish_notice(self, notes, added=0):
        """Всплывающий итог: что закончилось или не сработало."""
        if not notes:
            return
        names = leadfinder.SOURCE_NAMES
        text = "; ".join(f"{names.get(k, k)} — {str(v).rstrip('. ')}" for k, v in notes.items())
        tail = f" Остальные источники отработали: +{added}." if added else ""
        self.notice = {"seq": self.notice["seq"] + 1, "text": f"Не сработало: {text}.{tail}"}

    def start_analyze(self):
        """Кнопка «Проверить сайты»: сайты уже собранных лидов, которые ещё не
        проверялись. Та же круглая кнопка показывает ход и останавливает."""
        ok, msg, st = self._allowed()
        if not ok:
            return False, msg
        self.apply_own_keys(st)
        self.stop_event.clear()
        self.added = 0
        self.running = True
        self.run_status = "running"
        s = dict(self.config)
        s["proxies"] = [p.strip() for p in str(s.get("proxies") or "").splitlines() if p.strip()]

        def job():
            status = "idle"
            try:
                res = leadfinder.analyze_existing(s, self.store, self.log, self.stop_event) or {}
                self._finish_notice(res.get("notes") or {})
                self.push_to_server()
            except Exception as e:
                self.log(f"[site] СБОЙ проверки: {e}")
                status = "error"
            finally:
                self.run_status = status
                self.running = False

        self.thread = threading.Thread(target=job, daemon=True)
        self.thread.start()
        return True, "проверка сайтов запущена"

    def start(self, settings):
        ok, msg, st = self._allowed()
        if not ok:
            return False, msg

        self.save_config(settings)
        self.apply_own_keys(st)
        self.stop_event.clear()
        self.added = 0
        self.running = True
        self.run_status = "running"

        s = dict(self.config)
        s["proxies"] = [p.strip() for p in str(s.get("proxies") or "").splitlines() if p.strip()]

        def progress():
            self.added += 1

        def job():
            status = "idle"
            try:
                self.log("=== запуск сбора ===")
                self.cloud_note_run()
                res = leadfinder.collect(s, self.store, self.log, self.stop_event, progress) or {}
                notes = res.get("notes") or {}
                # Красная кнопка — только когда не собралось ничего. Если один
                # источник кончился, а другие отработали, это не ошибка прогона.
                if (res.get("blocked") or notes) and not res.get("added"):
                    status = "error"
                self._finish_notice(notes, res.get("added") or 0)
                self.push_to_server()
                if self.config.get("cleanup_on_finish"):
                    self.run_cleanup()
                self.log("=== сбор завершён ===")
            except Exception as e:
                self.log(f"СБОЙ: {e}")
                status = "error"
            finally:
                self.run_status = status
                self.running = False

        self.thread = threading.Thread(target=job, daemon=True)
        self.thread.start()
        return True, "запущено"

    def stop(self):
        self.stop_event.set()
        self.log("остановка по запросу…")
        return True

    # ---------- связь с сервером ----------
    OWN_KEY_FIELDS = {"2gis": "gis_api_key", "yandex": "yandex_api_key",
                      "vk": "vk_token", "pagespeed": "pagespeed_key"}

    def apply_own_keys(self, st=None):
        """Свои ключи менеджера — только если владелец это разрешил. Уходят на
        сервер вместе с запросом; серверный ключ сюда не приходит никогда."""
        st = st or self.cloud.state or {}
        keys = {}
        if st.get("allow_own_keys", True):
            for src, field in self.OWN_KEY_FIELDS.items():
                v = str(self.config.get(field) or "").strip()
                if v:
                    keys[src] = v
        leadfinder.OWN_KEYS = keys

    def cloud_note_run(self):
        """Отметить прогон на сервере — для журнала (лимита прогонов нет)."""
        try:
            self.cloud._request("session.php", {"run_start": 1})
        except Exception:
            pass

    def push_to_server(self):
        """Отправить собранное в общую базу. Стоп-лист сервер применит сам:
        отказавшиеся не вернутся, даже если пришли из другого источника."""
        if not self.cloud.logged_in:
            return
        # Только то, что изменилось в этом окне. Раньше уходила вся база, и
        # старая копия одного менеджера затирала свежие данные другого.
        leads = [l for l in self.store.dirty() if not l.get("deleted")]
        if not leads:
            return
        try:
            r = self.cloud.push(leads)
            self.store.clear_dirty([l["id"] for l in leads])
            msg = f"на сервер отправлено: новых {r.get('added', 0)}, обновлено {r.get('updated', 0)}"
            if r.get("blocked"):
                msg += f", отсеяно стоп-листом {r['blocked']}"
            self.log(msg)
        except Exception as e:
            self.log(f"не удалось отправить на сервер: {e}. Данные остались только в этом окне —"
                     f" не закрывай приложение до восстановления связи.")

    def pull_from_server(self):
        """Забрать общую базу в память окна. Без этого после перезапуска список
        был бы пуст: на диске у менеджера базы больше нет."""
        if not self.cloud.logged_in:
            return 0
        try:
            rows = self.cloud.pull()
            # Корзина тоже живёт на сервере — без неё после перезапуска она была пустой.
            try:
                rows += self.cloud.pull(view="deleted")
            except Exception:
                pass
            n = self.store.load_many(rows)
            self.log(f"с сервера загружено лидов: {n}")
        except Exception as e:
            self.log(f"не удалось загрузить базу с сервера: {e}")
            return 0
        # Оценки на сервере могли посчитаться старой формулой (например, ложные
        # «горячие» с непроверенным сайтом). Пересчитываем и отправляем только
        # те, у которых оценка изменилась.
        changed = 0
        for l in self.store.all():
            before = (l.get("score"), l.get("status"))
            leadfinder.compute_score(l)
            if (l.get("score"), l.get("status")) != before:
                self.store.mark_dirty(l["id"])
                changed += 1
        if changed:
            self.log(f"оценка пересчитана по новым правилам: {changed} лидов — отправляю на сервер")
            threading.Thread(target=self.push_to_server, daemon=True).start()
        return n

    def load_stoplist(self):
        """Подтянуть стоп-лист с сервера в фильтр сбора."""
        if not self.cloud.logged_in:
            return 0
        try:
            d = self.cloud.stoplist()
            c = d.get("compact") or {}
            leadfinder.STOPLIST = {
                "lead": set(c.get("lead") or []),
                "phone": set(c.get("phone") or []),
                "domain": set(c.get("domain") or []),
                "word": [w for w in (c.get("word") or []) if w],
            }
            return int(d.get("count") or 0)
        except Exception as e:
            self.log(f"стоп-лист не загрузился: {e}")
            return 0

    def beat(self):
        """Спросить сервер и сразу исполнить его решение.

        Отдельно от cloud.beat(): там только опрос. Команду на самоуничтожение
        нельзя откладывать до следующего тика таймера — она должна сработать
        при любом обращении к серверу, включая ручную проверку из окна.
        """
        st = self.cloud.beat()
        if self.cloud.wipe_requested and not self.wipe:
            self.on_wipe(self.cloud.wipe_installer)
        return st

    def start_heartbeat(self):
        """Держим связь с сервером: раз в 30 секунд спрашиваем разрешение.
        Запускается один раз — после входа или при старте с сохранённым входом."""
        if getattr(self, "_beat_started", False):
            return
        self._beat_started = True
        self.cloud.start_heartbeat(30, on_wipe=self.on_wipe)

    def on_wipe(self, with_installer):
        """Сервер отозвал доступ: стираем всё, что есть локально, и сообщаем
        Electron — он доудалит саму программу."""
        self.wipe = {"installer": bool(with_installer)}
        try:
            self.store.clear_all()
        except Exception:
            pass
        self.cloud.forget()
        try:
            os.remove(CONFIG_PATH)
        except Exception:
            pass
        self.log("ДОСТУП ОТОЗВАН — данные стёрты, приложение будет удалено")

    def run_cleanup(self):
        try:
            import cleanup
            cfg = {"CLEANUP_SAMPLES": True, "CLEANUP_CONTEXTS": True, "CLEANUP_TEMP": True,
                   "CLEANUP_DNS": True, "CONTEXT_DIR": "browser_contexts"}
            cleanup.cleanup_traces(cfg, log=lambda m: self.log(f"[cleanup] {m}"))
        except Exception as e:
            self.log(f"[cleanup] не удалось: {e}")

    # ---------- демо-данные (чтобы увидеть интерфейс без интернета/ключа) ----------
    def seed_demo(self):
        samples = _DEMO_LEADS()
        for l in samples:
            leadfinder.compute_score(l)
            self.store.upsert_one(l)
        self.log(f"загружены демо-данные: {len(samples)} лидов (это пример, не реальный сбор)")
        return len(samples)


def _DEMO_LEADS():
    def mk(name, ind, phone, site, reachable, ssl, mobile, cms, rating, reviews, socials=None, has_form=False):
        l = leadfinder.blank_lead()
        l.update({"name": name, "industry": ind, "city": "Казань", "address": "ул. Примерная, 1",
                  "phone": phone, "website": site, "rating": rating, "reviews_count": reviews,
                  "socials": socials or [], "source": "demo"})
        if site:
            l["site"] = {"reachable": reachable, "ssl": ssl, "mobile": mobile, "cms": cms,
                         "speed_ms": 1800, "has_form": has_form, "has_booking": False,
                         "seo": ssl, "cta": has_form, "_done": True}
        l["_rubrics"] = [ind]
        return l
    return [
        mk("Барбершоп «Бритва»", "Барбершоп", "+7 843 200-11-01", None, False, False, False, None, 4.8, 63,
           socials=["vk.com/britva"]),
        mk("Кофейня «Зерно»", "Кофейня", "+7 843 200-11-02", None, False, False, False, None, 4.6, 121,
           socials=["instagram.com/zerno"]),
        mk("Автосервис «Гараж 16»", "Автосервис", "+7 843 200-11-03", "http://garage16.ru", True, False, False,
           "Joomla", 4.2, 34),
        mk("Стоматология «Улыбка»", "Стоматология", "+7 843 200-11-04", "http://ulybka-kzn.ru", True, True, False,
           "1C-Bitrix", 4.9, 210),
        mk("Салон красоты «Локон»", "Салон красоты", "+7 843 200-11-05", None, False, False, False, None, 4.3, 45),
        mk("Пекарня «Каравай»", "Пекарня", "+7 843 200-11-06", "http://karavay16.ru", False, False, False, None, 4.1, 12),
        mk("Фитнес «Тонус»", "Фитнес-клуб", "+7 843 200-11-07", "https://tonus-fit.ru", True, True, True, "Tilda", 4.7, 88,
           has_form=True),
        mk("Юрбюро «Право»", "Юридические услуги", "+7 843 200-11-08", "http://pravo-kzn.ru", True, False, True, "WordPress",
           4.0, 27),
        mk("Цветы «Флора»", "Магазин цветов", "+7 843 200-11-09", None, False, False, False, None, 4.5, 71,
           socials=["vk.com/flora16"]),
        mk("Ремонт обуви «Каблучок»", "Ремонт обуви", "+7 843 200-11-10", None, False, False, False, None, 4.4, 19),
        mk("Клининг «Чисто»", "Клининговые услуги", "+7 843 200-11-11", "http://chisto-kzn.ru", True, False, False, "DLE",
           4.2, 38),
        mk("Ветклиника «Друг»", "Ветеринария", "+7 843 200-11-12", "https://drug-vet.ru", True, True, True, "Webflow", 4.8,
           156, has_form=True),
        mk("Шиномонтаж «Колесо»", "Шиномонтаж", "+7 843 200-11-13", None, False, False, False, None, 4.0, 8),
        mk("Кондитерская «Сахар»", "Кондитерская", "+7 843 200-11-14", None, False, False, False, None, 4.6, 94,
           socials=["instagram.com/sahar.kzn"]),
    ]


def _safe_url(v):
    """Из чужого JSON может прийти javascript:/file:-ссылка — оставляем только http(s)."""
    s = str(v or "").strip()
    return s if re.match(r"^https?://", s, re.I) else None


def _import_to_lead(item):
    """Приводит произвольный объект из пользовательского JSON к схеме лида."""
    l = leadfinder.blank_lead()
    for k in ("name", "industry", "city", "address", "phone", "email", "website",
              "rating", "reviews_count", "hours", "recommendation", "source", "scraped_at"):
        if item.get(k) is not None:
            l[k] = item[k]
    for k in ("socials", "messengers", "reasons", "_rubrics"):
        v = item.get(k)
        if isinstance(v, list):
            l[k] = v
        elif isinstance(v, str) and v.strip():
            l[k] = [x.strip() for x in v.split(";") if x.strip()]
    if not l.get("source"):
        l["source"] = "import"
    l["website"] = _safe_url(l.get("website"))
    for k in ("socials", "messengers"):
        l[k] = [u for u in (_safe_url(x) for x in (l.get(k) or [])) if u]
    # сайт-метрики (плоский экспорт) -> вложенный site
    site = {}
    for sk, dk in (("site_reachable", "reachable"), ("site_ssl", "ssl"), ("site_mobile", "mobile"),
                   ("site_speed_ms", "speed_ms"), ("site_pagespeed", "pagespeed"), ("site_cms", "cms"),
                   ("site_has_form", "has_form"), ("site_has_booking", "has_booking"),
                   ("site_seo", "seo"), ("site_cta", "cta")):
        if item.get(sk) is not None:
            site[dk] = item[sk]
    if isinstance(item.get("site"), dict):
        site.update(item["site"])
    if site:
        l["site"] = site
    # скор: если в файле уже есть — сохраняем, иначе считаем
    if item.get("score") is not None:
        l["score"] = item["score"]
        l["status"] = item.get("status") or l["status"]
    else:
        leadfinder.compute_score(l)
    for f in ("favorite", "checked"):
        if isinstance(item.get(f), bool):
            l[f] = item[f]
    return l


APP = App()

# Пропуск с прошлого запуска — сразу проверяем, действует ли он ещё. Если доступ
# отозвали, пока приложение было закрыто, команда на удаление придёт тут же.
if APP.cloud.logged_in:
    APP.beat()
    if not APP.wipe:
        APP.load_stoplist()
        APP.pull_from_server()
        APP.start_heartbeat()


# --------------------------------------------------------------------------
#  HTTP-обработчик
# --------------------------------------------------------------------------
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    # разделы «Календарь» и «CRM» внутри окна
    "/views.js": ("views.js", "application/javascript; charset=utf-8"),
}


# Секрет на запуск, который передаёт Electron. Если его нет (ручной `python server.py`) —
# проверка выключена, режим «как раньше».
AUTH_TOKEN = os.environ.get("BISQUARE_TOKEN") or ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: меньше коннектов → нет случайных ERR_CONNECTION_REFUSED

    def log_message(self, *a):
        pass  # тихо

    def _authorized(self):
        """Пускаем к /api/* только своё окно: заголовок или ?token= .
        Защищает от POST-ов с посторонних веб-страниц (прочитать ответ они не могут
        из-за отсутствия CORS, а вот что-нибудь испортить — смогли бы)."""
        if not AUTH_TOKEN:
            return True
        if self.headers.get("X-Bisquare-Token") == AUTH_TOKEN:
            return True
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (qs.get("token") or [""])[0] == AUTH_TOKEN

    # ---- утилиты ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _q1(self, qs, key, default=""):
        return (qs.get(key) or [default])[0]

    def _int1(self, qs, key, default=0):
        try:
            return max(0, min(100, int(self._q1(qs, key, default))))
        except (TypeError, ValueError):
            return default

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/") and not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if path in _STATIC:
            fname, ctype = _STATIC[path]
            fp = os.path.join(WEB_DIR, fname)
            try:
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ctype)
            except FileNotFoundError:
                return self._send(404, {"error": f"нет файла {fname}"})

        if path == "/api/state":
            lines, seq = APP.log_tail()
            status = "running" if APP.running else APP.run_status
            return self._send(200, {"running": APP.running, "status": status, "added": APP.added,
                                    "notice": APP.notice,
                                    "stats": APP.store.stats(), "log": lines, "seq": seq,
                                    # Состояние доступа — по нему окно решает, что показывать.
                                    "auth": {
                                        "logged_in": APP.cloud.logged_in,
                                        "user": APP.cloud.user,
                                        "server": APP.cloud.base_url,
                                        **APP.cloud.state,
                                    },
                                    "wipe": APP.wipe})

        if path == "/api/config":
            return self._send(200, APP.config)

        if path == "/api/leads":
            qs = self._qs()
            leads = APP.store.list(view=self._q1(qs, "view", "all"), sort=self._q1(qs, "sort", "score"),
                                   direction=self._q1(qs, "dir", "desc"), q=self._q1(qs, "q", ""),
                                   status=self._q1(qs, "status", ""), show=self._q1(qs, "show", ""),
                                   min_score=self._int1(qs, "min", 0))
            return self._send(200, {"leads": leads, "count": len(leads), "stats": APP.store.stats()})

        if path == "/api/export/status":
            return self._send(200, APP.export_status(self._q1(self._qs(), "id", "")))

        if path == "/api/export":
            # Файл отдаём только по одноразовому ключу, который выдаётся после
            # согласия владельца, и ровно по тем фильтрам, о которых его спросили.
            a = APP.export_keys.pop(self._q1(self._qs(), "key", ""), None)
            if not a:
                return self._send(403, {"ok": False, "msg": "выгрузка не разрешена — запроси её заново"})
            fmt = a.get("format") or "json"
            data, ct, fname, n = APP.store.export_bytes(
                fmt=fmt, view=a.get("view") or "all", sort=a.get("sort") or "score",
                direction=a.get("dir") or "desc", q=a.get("q") or "", status=a.get("status") or "",
                show=a.get("show") or "", min_score=int(a.get("min") or 0))
            # заодно кладём копию в папку экспорта из конфига
            try:
                out_dir = APP.config.get("out_dir") or DATA_DIR
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, fname), "wb") as f:
                    f.write(data)
                APP.log(f"экспорт: {n} лидов -> {os.path.join(out_dir, fname)}")
            except Exception as e:
                APP.log(f"экспорт: не удалось сохранить копию ({e})")
            return self._send(200, data, ct, {"Content-Disposition": f'attachment; filename="{fname}"'})

        return self._send(404, {"error": "unknown"})

    # ---- POST ----
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/") and not self._authorized():
            return self._send(403, {"error": "forbidden"})
        body = self._body_json()

        # ---- вход и связь с сервером ----
        if path == "/api/auth/login":
            try:
                user = APP.cloud.login((body or {}).get("server") or APP.config.get("server_url"),
                                       (body or {}).get("login", ""),
                                       (body or {}).get("password", ""))
            except cloud_mod.CloudError as e:
                return self._send(200, {"ok": False, "msg": str(e), "error": e.code})
            APP.save_config({"server_url": APP.cloud.base_url})
            n = APP.load_stoplist()
            APP.log(f"стоп-лист загружен: {n} записей")
            APP.pull_from_server()
            APP.start_heartbeat()
            return self._send(200, {"ok": True, "user": user, "state": APP.cloud.state})

        if path == "/api/auth/logout":
            # База живёт в памяти, поэтому перед выходом досылаем собранное:
            # иначе выход посреди работы потерял бы лиды, которые ещё не ушли.
            sent = True
            try:
                APP.push_to_server()
            except Exception:
                sent = False
            APP.cloud.forget()
            APP.store.clear_all()     # в окне не должно остаться чужих клиентов
            APP.log("выход из учётной записи")
            return self._send(200, {"ok": True, "synced": sent})

        if path == "/api/auth/refresh":
            st = APP.beat()
            return self._send(200, {"ok": True, "state": st})

        # ---- работа со звонками: всё решает сервер, окно только показывает ----
        if path == "/api/contact":
            b = body or {}
            lid = b.get("lead_id", "")
            call = lambda: APP.cloud.contact(lid, b.get("action", ""),
                                             b.get("comment", ""), b.get("next_at", ""))
            try:
                d = call()
            except cloud_mod.CloudError as e:
                # Лид собран в этом окне, но ещё не ушёл на сервер (сбор не
                # закончился). Досылаем эту карточку и повторяем.
                local = APP.store._leads.get(lid)
                if e.code == "lead_not_found" and local:
                    try:
                        APP.cloud.push([local])
                        d = call()
                    except cloud_mod.CloudError as e2:
                        return self._send(200, {"ok": False, "msg": str(e2)})
                else:
                    return self._send(200, {"ok": False, "msg": str(e)})
            # Локальный кеш подвинем следом, чтобы список не показывал старое.
            lid = b.get("lead_id")
            l = APP.store._leads.get(lid) if lid else None
            if l is not None:
                l["call_status"] = d.get("call_status") or l.get("call_status")
                if d.get("removed"):
                    l["deleted"] = True
                if d.get("favorite"):
                    l["favorite"] = True
            return self._send(200, d)

        # ---- календарь и CRM внутри приложения ----
        # Окно приложения не ходит на сервер само: запросы идут отсюда, с токеном
        # устройства. Права проверяет сервер — менеджер получит ровно то, что ему
        # положено, как и на сайте.
        if path == "/api/cloud/call":
            b = body or {}
            target = str(b.get("path") or "")
            if target not in ("crm.php", "bookings.php"):
                return self._send(200, {"ok": False, "msg": "недоступный раздел"})
            query = b.get("query") or {}
            q = urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")}) \
                if isinstance(query, dict) else ""
            payload = b.get("body")
            try:
                d = APP.cloud._request(target + ("?" + q if q else ""),
                                       payload if isinstance(payload, dict) else None,
                                       method="POST" if isinstance(payload, dict) else "GET")
            except cloud_mod.CloudError as e:
                return self._send(200, {"ok": False, "msg": str(e), "error": e.code})
            return self._send(200, d)

        if path == "/api/sync":
            APP.push_to_server()
            APP.load_stoplist()
            return self._send(200, {"ok": True, "stats": APP.store.stats()})

        if path == "/api/run":
            ok, msg = APP.start(body or {})
            return self._send(200, {"ok": ok, "msg": msg})

        if path == "/api/stop":
            APP.stop()
            return self._send(200, {"ok": True})

        if path == "/api/config":
            APP.save_config(body or {})
            return self._send(200, {"ok": True, "config": APP.config})

        if path == "/api/lead":
            qs = self._qs()
            lid = self._q1(qs, "id") or (body or {}).get("id")
            action = self._q1(qs, "action") or (body or {}).get("action")
            ok = APP.store.set_flag(lid, action)
            # Отметка должна пережить перезапуск — отправляем её на сервер.
            # Не получилось (лида ещё нет там) — досылаем карточку и пробуем снова.
            synced = True
            if ok and APP.cloud.logged_in:
                try:
                    APP.cloud.flag(lid, action)
                except cloud_mod.CloudError as e:
                    local = APP.store._leads.get(lid)
                    synced = False
                    if e.code == "lead_not_found" and local:
                        try:
                            APP.cloud.push([local])
                            APP.cloud.flag(lid, action)
                            synced = True
                        except cloud_mod.CloudError:
                            pass
            return self._send(200, {"ok": ok, "synced": synced, "stats": APP.store.stats()})


        if path == "/api/export/request":
            return self._send(200, APP.export_request(body or {}))

        if path == "/api/export/claim":
            return self._send(200, APP.export_claim(str((body or {}).get("id") or "")))

        if path == "/api/analyze":
            ok, msg = APP.start_analyze()
            return self._send(200, {"ok": ok, "msg": msg})

        if path == "/api/purge":
            # После входа корзина общая и живёт на сервере — стирать её из окна
            # нельзя (у менеджера и кнопки нет). Удалённое можно только вернуть.
            if APP.cloud.logged_in:
                return self._send(200, {"ok": False, "msg": "корзина хранится на сервере — оттуда её не очистить"})
            n = APP.store.purge_deleted()
            APP.log(f"корзина очищена: удалено {n}")
            return self._send(200, {"ok": True, "removed": n, "stats": APP.store.stats()})

        if path == "/api/clear":
            # После входа база общая и живёт на сервере — окно её не стирает.
            if APP.cloud.logged_in:
                return self._send(200, {"ok": False, "msg": "база хранится на сервере — из окна её не очистить"})
            n = APP.store.clear_all()
            APP.log(f"база очищена: удалено {n}")
            return self._send(200, {"ok": True, "removed": n, "stats": APP.store.stats()})

        if path == "/api/log/clear":
            n = APP.clear_log()
            return self._send(200, {"ok": True, "removed": n})

        if path == "/api/cleanup":
            threading.Thread(target=APP.run_cleanup, daemon=True).start()
            return self._send(200, {"ok": True})

        if path == "/api/demo":
            # Демо-лиды после входа уехали бы в общую базу на сервере.
            if APP.cloud.logged_in:
                return self._send(200, {"ok": False, "msg": "демо-данные — только без входа, чтобы не засорить общую базу"})
            n = APP.seed_demo()
            return self._send(200, {"ok": True, "added": n, "stats": APP.store.stats()})

        if path == "/api/import":
            items = body.get("leads") or body.get("items") if isinstance(body, dict) else body
            if not isinstance(items, list):
                return self._send(200, {"ok": False, "msg": "ожидался JSON-массив лидов или {\"leads\":[...]}"})
            added, updated, skipped = 0, 0, 0
            for it in items:
                if not isinstance(it, dict) or not (it.get("name") or it.get("phone") or it.get("website")):
                    skipped += 1
                    continue
                _, is_new = APP.store.upsert_one(_import_to_lead(it))
                added += 1 if is_new else 0
                updated += 0 if is_new else 1
            APP.log(f"импорт JSON: получено {len(items)}, добавлено {added}, обновлено {updated}, пропущено {skipped}")
            return self._send(200, {"ok": True, "received": len(items), "added": added,
                                    "updated": updated, "skipped": skipped, "stats": APP.store.stats()})

        if path == "/api/probe":
            # проверка ключа/квоты одним запросом (region/search) — не жжёт целый прогон
            key = (APP.config.get("gis_api_key") or "").strip()
            city = ((APP.config.get("cities") or "Казань").splitlines() or ["Казань"])[0].strip() or "Казань"
            APP.apply_own_keys()
            leadfinder.DEAD_SOURCES.discard("2gis")
            if not leadfinder.key_ready("2gis", key):
                return self._send(200, {"ok": False, "status": "no_key",
                                        "msg": "2ГИС: " + leadfinder.key_missing("2gis")})
            b = leadfinder.Budget(2)
            rid = leadfinder.resolve_region(city, key, lambda m: APP.log("[probe] " + m), b)
            status = "blocked" if b.blocked else ("ok" if rid else "invalid")
            msg = {"ok": f"ключ работает (регион {rid}), запросов потрачено {b.used}",
                   "blocked": "ключ ЗАБЛОКИРОВАН/лимит — месячная квота 2ГИС исчерпана",
                   "invalid": "ключ неверный/неактивный (нужен Catalog API)"}[status]
            APP.log(f"[probe] статус ключа: {status}")
            return self._send(200, {"ok": status == "ok", "status": status, "region": rid, "msg": msg})

        if path == "/api/export_dir":
            qs = self._qs()
            out_dir = APP.config.get("out_dir") or DATA_DIR
            p, n = APP.store.export_to_dir(out_dir, fmt=self._q1(qs, "format", "json"),
                                           view=self._q1(qs, "view", "all"))
            APP.log(f"экспорт в папку: {n} лидов -> {p}")
            return self._send(200, {"ok": True, "path": p, "count": n})

        return self._send(404, {"error": "unknown"})


def open_app_window(url):
    """Открыть как ПРИЛОЖЕНИЕ — отдельное окно без вкладок/адресной строки
    (Edge/Chrome в режиме --app). Если не нашли — обычный браузер."""
    import shutil
    import subprocess
    prof = os.path.join(APP_DIR, ".appwin")   # свой профиль, чтобы окно было чистым
    candidates = [
        "msedge", "chrome", "google-chrome", "chromium",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        exe = c if os.path.isabs(c) and os.path.exists(c) else shutil.which(c)
        if not exe:
            continue
        try:
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}",
                              "--window-size=1120,760", "--no-first-run", "--no-default-browser-check"])
            return
        except Exception:
            continue
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    class BisServer(ThreadingHTTPServer):
        daemon_threads = True          # потоки-обработчики не держат процесс
        allow_reuse_address = True
        request_queue_size = 128       # длинная очередь входящих коннектов

    srv = BisServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[bisquare] интерфейс: {url}", flush=True)
    APP.log(f"сервер запущен на {url}")
    if not args.no_open:
        open_app_window(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bisquare] остановлено", flush=True)


if __name__ == "__main__":
    main()
