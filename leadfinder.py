"""Bisquare Lead Finder — самостоятельный движок сбора лидов.

Находит микро/мини-бизнес в РФ через 2ГИС Catalog API, опционально проверяет
веб-присутствие настоящим стелс-браузером (antibot.py — те же обходы, что в боте),
считает скоринг «нужен ли сайт» (0–100) и складывает лиды в общий store.

Внутренних зависимостей нет: 2ГИС — через urllib (синхронно), анализ сайтов —
через Playwright + antibot (опционально, включается галкой).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime

# --------------------------------------------------------------------------
#  HTTP к официальным API (2ГИС, PageSpeed) — синхронно
# --------------------------------------------------------------------------
_SSL_CTX = ssl.create_default_context()


def http_json(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BisquareLeadFinder/2.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# --------------------------------------------------------------------------
#  Запросы к источникам: через сервер или напрямую
#
#  Источники с ключом (2ГИС, Яндекс, ВК, PageSpeed) ходят через сервер: ключ
#  подставляет он, на машину менеджера ключ не попадает и вытащить его из
#  приложения нельзя. Источники без ключа (OSM, Overpass, Nominatim) идут
#  напрямую — гонять их через сервер незачем.
#
#  CLOUD ставит server.py после входа. Если его нет (запуск без сервера),
#  работает прежний путь с локальным ключом.
# --------------------------------------------------------------------------
CLOUD = None          # cloud.Cloud
OWN_KEYS = {}         # свои ключи менеджера, если он их вписал: {"2gis": "..."}
DEAD_SOURCES = set()  # кому сервер отказал насовсем в этом прогоне — больше не стучимся
SOURCE_NOTES = {}     # почему источник остановился — для итога прогона: {"2gis": "квота …"}
SOURCE_NAMES = {"2gis": "2ГИС", "osm": "OSM", "yandex": "Яндекс", "vk": "ВК", "pagespeed": "PageSpeed"}


def source_down(src, why):
    """Источник кончился до конца прогона (квота, ключ, отказ). Остальные
    продолжают работать, а причина попадёт в итог — человек увидит, что именно
    закончилось."""
    DEAD_SOURCES.add(src)
    SOURCE_NOTES.setdefault(src, str(why)[:200])

# Отказы сервера, после которых повторять бессмысленно до конца прогона.
_FATAL = {"quota_exhausted", "no_key", "own_key_required", "search_blocked",
          "device_revoked", "no_device_token", "bad_source", "endpoint_not_allowed"}

_DIRECT_BASE = {
    "2gis":      "https://catalog.api.2gis.com/",
    "yandex":    "https://search-maps.yandex.ru/",
    "vk":        "https://api.vk.com/",
    "pagespeed": "https://www.googleapis.com/",
}
_KEY_PARAM = {"2gis": "key", "yandex": "apikey", "vk": "access_token", "pagespeed": "key"}

# Последняя причина отказа сервера — чтобы написать её в журнал понятным текстом.
last_source_error = ""


def via_server():
    return CLOUD is not None and getattr(CLOUD, "logged_in", False)


def key_ready(source, local_key=""):
    """Есть ли чем ходить в источник.

    После входа ключ — забота сервера: свой ключ менеджера (если владелец это
    разрешил) или серверный. Значение серверного ключа приложение не знает —
    только «есть/нет» из ответа сервера."""
    if not via_server():
        return bool((local_key or "").strip())
    st = getattr(CLOUD, "state", None) or {}
    mode = st.get("keys_mode", "own_or_server")
    own = bool(OWN_KEYS.get(source))
    srv = bool((st.get("server_sources") or {}).get(source))
    if mode == "own":
        return own
    if mode == "server":
        return srv
    return own or srv


def key_missing(source):
    """Почему у источника нет ключа — для журнала, человеческим языком."""
    if not via_server():
        return "не вписан ключ"
    mode = (getattr(CLOUD, "state", None) or {}).get("keys_mode", "own_or_server")
    if mode == "own":
        return "нужен свой ключ (так настроил владелец)"
    if mode == "server":
        return "на сервере нет ключа — попроси владельца завести его в панели"
    return "ключа нет ни у тебя, ни на сервере"


_KEY_NAMES = (("2gis", "2ГИС"), ("yandex", "Яндекс"), ("vk", "ВК"), ("pagespeed", "PageSpeed"))
_MODE_NAMES = {"own": "только свои", "server": "только серверные",
               "own_or_server": "свой, иначе серверный"}


def keys_summary():
    """Строки для журнала в начале прогона: чей ключ у каждого источника.
    Без неё непонятно, почему источник молчит — нет ключа или режим не пускает."""
    if not via_server():
        return []
    st = getattr(CLOUD, "state", None) or {}
    mode = st.get("keys_mode", "own_or_server")
    srv = st.get("server_sources") or {}
    parts, hidden = [], []
    for src, name in _KEY_NAMES:
        if mode != "server" and OWN_KEYS.get(src):
            who = "свой"
        elif mode != "own" and srv.get(src):
            who = "сервер"
        else:
            who = "нет"
            if mode == "own" and srv.get(src):
                hidden.append(name)
        parts.append(f"{name} — {who}")
    out = [f"ключи (режим «{_MODE_NAMES.get(mode, mode)}»): " + " · ".join(parts)]
    if hidden:
        out.append(f"на сервере есть ключ {', '.join(hidden)}, но режим «только свои» его не отдаёт "
                   f"(режим меняет владелец: панель → Настройки → «Чей ключ тратится при поиске»)")
    return out


def source_json(source, endpoint, params, timeout=25, log=None):
    """Единая точка обращения к внешнему источнику."""
    global last_source_error
    params = dict(params or {})

    if via_server() and source in _DIRECT_BASE:
        if source in DEAD_SOURCES:
            return None
        # Ключ серверу не отправляем: он подставит свой или ключ менеджера.
        params.pop(_KEY_PARAM.get(source, "key"), None)
        for attempt in range(3):
            try:
                return CLOUD.proxy(source, endpoint, params, OWN_KEYS.get(source, ""),
                                   timeout=max(40, timeout + 25))
            except Exception as e:
                code = getattr(e, "code", "")
                last_source_error = str(e)
                if code == "rate_limited" and attempt < 2:
                    if log:
                        log(f"[{source}] сервер просит притормозить — жду 20 с")
                    time.sleep(20)
                    continue
                if code in _FATAL:
                    source_down(source, e)
                if log:
                    log(f"[{source}] сервер отказал: {e}")
                return None
        return None

    base = _DIRECT_BASE.get(source)
    if not base:
        return None
    return http_json(base + endpoint + "?" + urllib.parse.urlencode(params, doseq=True), timeout)


# --------------------------------------------------------------------------
#  Экономия квоты 2ГИС: бюджет запросов, детект блокировки, кэш региона/рубрик
#  (у бесплатного тарифа всего 1000 запросов В МЕСЯЦ, и даже 404 списывается!)
# --------------------------------------------------------------------------
_BLOCK_RX = re.compile(r"(block|forbidden|denied|quota|limit|подписк|заблок|лимит|превыш|доступ)", re.I)


class Budget:
    """Жёсткий предохранитель: сколько обращений к 2ГИС можно сделать за прогон."""
    def __init__(self, cap):
        self.cap = cap if (cap and cap > 0) else 0    # 0 = без лимита
        self.used = 0
        self.blocked = False

    def allow(self):
        return (not self.blocked) and (self.cap == 0 or self.used < self.cap)

    def hit(self):
        self.used += 1


def _meta_error(data):
    meta = (data or {}).get("meta") or {}
    return meta.get("code"), ((meta.get("error") or {}).get("message") or "")


def _is_block(code, msg):
    return (code in (401, 402, 403, 429)) or bool(_BLOCK_RX.search(msg or ""))


_BAD_KEY_RX = re.compile(r"(incorrect key|invalid key|key is invalid|неверн\w* ключ)", re.I)


def _block_text(code, msg):
    """Что сказать в журнале при отказе 2ГИС: неверный ключ и кончившаяся
    квота выглядят одинаково (403), а лечатся по-разному."""
    if _BAD_KEY_RX.search(msg or ""):
        who = "в панели сервера" if via_server() else "в настройках"
        return f"ключ 2ГИС неверный ({code} {msg}) — проверь ключ {who}."
    return (f"ключ ЗАБЛОКИРОВАН/лимит ({code} {msg}). "
            f"Похоже, исчерпана месячная квота 2ГИС — подними лимит в кабинете или дождись сброса.")


def _cache_file():
    base = (os.environ.get("BISQUARE_DATA_DIR")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads_out"))
    return os.path.join(base, "gis_cache.json")


def _load_cache():
    try:
        with open(_cache_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c):
    try:
        os.makedirs(os.path.dirname(_cache_file()), exist_ok=True)
        with open(_cache_file(), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
    except Exception:
        pass


# --------------------------------------------------------------------------
#  Карточка лида
# --------------------------------------------------------------------------
def blank_lead():
    return {
        "name": None, "industry": None, "city": None, "address": None,
        "phone": None, "email": None, "website": None, "socials": [], "messengers": [],
        "rating": None, "reviews_count": None, "hours": None, "social_activity": None,
        "site": {}, "score": 0, "status": "cold", "reasons": [], "recommendation": None,
        "source": "2gis", "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "_rubrics": [], "_branch_count": None,
    }


_WEBSTUDIO_RX = re.compile(r"(веб[- ]?студи|web[- ]?studio|digital|создани[ея]\s+сайт|разработк[аи]\s+сайт|"
                           r"it[- ]?компан|интернет[- ]?агентств|маркетингов|реклам|seo|smm|брендинг)", re.I)
_CHAIN_RX = re.compile(r"(сеть|франшиз|официальн\w+ дилер)", re.I)
_MODERN_CMS = re.compile(r"(tilda|wix|readymag|webflow|bitrix24 sites|craftum|nethouse)", re.I)
_OLD_CMS = re.compile(r"(joomla|drupal 7|dle|ucoz|wordpress 4|html4|frontpage)", re.I)


def norm_domain(url):
    if not url:
        return None
    u = url.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    try:
        return urllib.parse.urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return None


# --------------------------------------------------------------------------
#  2ГИС Catalog API
# --------------------------------------------------------------------------
_GIS_FIELDS = ("items.point,items.name,items.full_name,items.address_name,items.contact_groups,"
               "items.rubrics,items.reviews,items.org,items.schedule,items.region_id")


def _gis_dead(budget):
    """Сервер отказал по 2ГИС насовсем (нет ключа, квота, стоп) — гасим источник
    через бюджет, чтобы остальные рубрики и города не стучались впустую.
    Причину source_json уже написал в журнал."""
    if "2gis" not in DEAD_SOURCES:
        return False
    if budget:
        budget.blocked = True
    return True


def resolve_region(city, key, log, budget=None):
    if budget and not budget.allow():
        return None
    if budget:
        budget.hit()
    data = source_json("2gis", "2.0/region/search", {"q": city, "key": key}, log=log)
    if data is None:
        if _gis_dead(budget):
            return None
        log(f"[2gis] region/search: нет ответа (сеть/HTTP или заблокирован ключ) для '{city}'")
        return None
    code, msg = _meta_error(data)
    if code and code != 200:
        if _is_block(code, msg):
            if budget:
                budget.blocked = True
            log(f"[2gis] region/search: {_block_text(code, msg)}")
            SOURCE_NOTES.setdefault("2gis", _block_text(code, msg))
        else:
            log(f"[2gis] region/search code={code} {msg} — неверный/неактивный ключ (нужен Catalog API, dev.2gis.com)")
        return None
    its = ((data or {}).get("result") or {}).get("items") or []
    if not its:
        log(f"[2gis] город '{city}' не найден в region/search (проверь написание)")
        return None
    rid = its[0].get("id")
    log(f"[2gis] {city} -> region_id {rid}")
    return rid


def get_rubrics(region_id, key, log, budget=None):
    if budget and not budget.allow():
        return []
    if budget:
        budget.hit()
    data = source_json("2gis", "2.0/catalog/rubric/list",
                       {"region_id": region_id, "key": key}, log=log)
    if data is None and _gis_dead(budget):
        return []
    code, msg = _meta_error(data)
    if code and code != 200:
        if _is_block(code, msg):
            if budget:
                budget.blocked = True
            log(f"[2gis] rubric/list: {_block_text(code, msg)}")
            SOURCE_NOTES.setdefault("2gis", _block_text(code, msg))
        return []
    its = ((data or {}).get("result") or {}).get("items") or []
    out = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("id") and node.get("name"):
                out.setdefault(node["id"], node["name"])
            for ch in (node.get("subrubrics") or node.get("items") or node.get("children") or []):
                walk(ch)
        elif isinstance(node, list):
            for n in node:
                walk(n)
    walk(its)
    rubrics = [{"id": i, "name": n} for i, n in out.items()]
    log(f"[2gis] рубрик получено: {len(rubrics)}")
    return rubrics


def gis_items(base, key, max_pages, log, label, stop, budget=None):
    """Пагинирует 3.0/items. ВАЖНО: у 2ГИС page_size максимум 10, и каждый запрос
    (в т.ч. пустой 404) списывается из месячной квоты."""
    items = []
    for page in range(1, max_pages + 1):
        if stop():
            break
        if budget and not budget.allow():
            if not budget.blocked:
                log(f"[2gis] бюджет запросов исчерпан ({budget.used}/{budget.cap}) — стоп, берегу квоту")
                SOURCE_NOTES.setdefault("2gis", f"кончился бюджет запросов на прогон ({budget.cap}) — "
                                                f"его можно поднять в Settings")
            break
        p = dict(base)
        p.update({"page": page, "page_size": 10, "key": key, "fields": _GIS_FIELDS, "locale": "ru_RU"})
        if budget:
            budget.hit()
        data = source_json("2gis", "3.0/items", p, log=log)
        if not data:
            if _gis_dead(budget):
                break
            log(f"[2gis] {label} стр.{page}: нет ответа (проверь ключ/лимиты)")
            break
        code, msg = _meta_error(data)
        if code and code != 200:
            if _is_block(code, msg):
                if budget:
                    budget.blocked = True
                log(f"[2gis] {label}: {_block_text(code, msg)} Стоп.")
                SOURCE_NOTES.setdefault("2gis", _block_text(code, msg))
            elif code != 404:   # 404 «Results not found» — просто пусто, тихо
                log(f"[2gis] {label}: API code={code} {msg}")
            break
        chunk = ((data.get("result") or {}).get("items")) or []
        if not chunk:
            break
        items.extend(chunk)
        time.sleep(0.4)
    return items


def collect_by_rubric(region_id, rub, key, max_pages, log, stop, city, budget=None, probe=None, seen_terms=None):
    """rubric_id → при 404/пусто фолбэк на q-поиск по названию рубрики.

    Экономно: rubric_id пробуем, только пока не доказано, что он не работает (одна неудача —
    и дальше сразу q). q-термины дедуплицируем в рамках города, чтобы не искать «Услуги»
    по пять раз. Всё под бюджетом запросов.
    """
    if probe is None:
        probe = {}
    if seen_terms is None:
        seen_terms = set()

    # 1) rubric_id — только если ещё не выяснили, что он бесполезен для этого ключа
    if probe.get("rid") is not False:
        items = gis_items({"rubric_id": rub["id"], "region_id": region_id}, key, max_pages, log,
                          f"{city}/{rub['name']}", stop, budget)
        if items:
            probe["rid"] = True
            return items
        if probe.get("rid") is None and not (budget and budget.blocked):
            probe["rid"] = False
            log("[2gis] rubric_id не отдаёт результатов — переключаюсь на q-поиск по названиям (экономлю запросы)")

    # 2) q-фолбэк по частям названия, с дедупом терминов в пределах города
    seen_ids, out = set(), []
    parts = [p.strip() for p in re.split(r"[\/,·|→>]+", rub["name"]) if p.strip()]
    for term in (parts or [rub["name"]]):
        t = term.lower()
        if t in seen_terms:
            continue
        seen_terms.add(t)
        if stop() or (budget and not budget.allow()):
            break
        got = gis_items({"q": term, "region_id": region_id}, key, max_pages, log,
                        f"{city}/q«{term}»", stop, budget)
        for it in got:
            iid = it.get("id") or (it.get("name"), it.get("address_name"))
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            out.append(it)
    return out


def parse_gis_item(it, city):
    lead = blank_lead()
    lead["name"] = it.get("name") or it.get("full_name")
    lead["address"] = it.get("address_name") or (it.get("full_name") or "")
    lead["city"] = city
    rubs = [r.get("name") for r in (it.get("rubrics") or []) if r.get("name")]
    lead["industry"] = rubs[0] if rubs else None
    lead["_rubrics"] = rubs
    rv = it.get("reviews") or {}
    lead["rating"] = rv.get("general_rating")
    lead["reviews_count"] = rv.get("general_review_count")
    org = it.get("org") or {}
    lead["_branch_count"] = org.get("branch_count")
    lead["hours"] = "есть" if it.get("schedule") else None
    socials, messengers = [], []
    for grp in (it.get("contact_groups") or []):
        for c in (grp.get("contacts") or []):
            t = (c.get("type") or "").lower()
            val = c.get("value") or c.get("url") or c.get("text")
            if not val:
                continue
            if t == "phone" and not lead["phone"]:
                lead["phone"] = val
            elif t == "email" and not lead["email"]:
                lead["email"] = val
            elif t == "website" and not lead["website"]:
                lead["website"] = val if re.match(r"^https?://", val, re.I) else ("http://" + val)
            elif t in ("vkontakte", "vk", "facebook", "instagram", "odnoklassniki", "youtube"):
                socials.append(val)
            elif t in ("whatsapp", "telegram", "viber"):
                messengers.append(val)
    lead["socials"] = socials
    lead["messengers"] = messengers
    lead["social_activity"] = "есть" if socials else None
    return lead


# Стоп-лист с сервера: кого не собирать ни при каких условиях. Заполняет
# server.py перед прогоном. Сервер проверяет его ещё раз при приёме — но
# отсеять здесь дешевле: не тратим квоту на анализ заведомо ненужных.
STOPLIST = {"lead": set(), "phone": set(), "domain": set(), "word": []}


def is_excluded(lead, extra_words):
    blob = f"{lead.get('name') or ''} {' '.join(lead.get('_rubrics') or [])}".lower()
    if _WEBSTUDIO_RX.search(blob):
        return "конкурент/веб-студия"
    for w in extra_words:
        if w and w.lower() in blob:
            return f"исключено по слову '{w}'"

    # Отказавшиеся клиенты и всё, что владелец закрыл вручную.
    phone = re.sub(r"\D", "", lead.get("phone") or "")
    if phone and phone in STOPLIST["phone"]:
        return "в стоп-листе (отказ)"
    dom = norm_domain(lead.get("website"))
    if dom and dom in STOPLIST["domain"]:
        return "в стоп-листе (отказ)"
    for w in STOPLIST["word"]:
        if w and w in blob:
            return f"в стоп-листе по слову '{w}'"
    return None


# --------------------------------------------------------------------------
#  Скоринг 0–100
# --------------------------------------------------------------------------
def site_checked(site):
    """Проверялся ли сайт хоть чем-то — браузером или PageSpeed."""
    return isinstance(site, dict) and (bool(site.get("_done")) or site.get("reachable") is not None)


def compute_score(lead):
    s, why = 0, []
    site = lead.get("site") if isinstance(lead.get("site"), dict) else {}
    has_site = bool(lead.get("website"))
    rating = lead.get("rating") or 0
    reviews = lead.get("reviews_count") or 0
    socials = bool(lead.get("socials"))

    # Проверка через PageSpeed (без браузера) не видит формы и CMS, а иногда и
    # адаптив: неизвестное там — None, и штрафовать за него нельзя.
    def known(k):
        return not (site.get("via") == "pagespeed" and site.get(k) is None)

    if not has_site:
        s += 35; why.append("нет сайта +35")
    elif not site_checked(site):
        # Про сайт ничего не известно — ни штрафов, ни бонусов. Раньше пустые
        # данные читались как «не открывается, нет адаптива, нет SSL» (+70),
        # и такие лиды ложно становились горячими.
        why.append("сайт не проверялся — оценка без него")
    else:
        if not site.get("reachable"):
            s += 30; why.append("сайт не открывается +30")
        if known("mobile") and not site.get("mobile"):
            s += 20; why.append("нет мобильной версии +20")
        if not site.get("ssl"):
            s += 10; why.append("нет SSL +10")
        slow = (site.get("pagespeed") is not None and site["pagespeed"] < 50) or \
               (site.get("pagespeed") is None and (site.get("speed_ms") or 0) > 3000)
        if slow:
            s += 15; why.append("медленный сайт +15")
        cms = site.get("cms") or ""
        if _OLD_CMS.search(cms) or (site.get("reachable") and known("seo") and not site.get("seo")):
            s += 10; why.append("устаревший дизайн/CMS +10")
        if known("has_form") and not (site.get("has_form") or site.get("has_booking")):
            s += 10; why.append("нет формы/онлайн-записи +10")

    if reviews and reviews > 0:
        s += 10; why.append("активный бизнес (есть отзывы) +10")
    if reviews and reviews >= 20 and rating and rating > 4:
        s += 5; why.append("много отзывов, рейтинг>4 +5")
    if socials and not has_site:
        s += 10; why.append("соцсети есть, сайта нет +10")

    if ((lead.get("_branch_count") or 0) > 10) or _CHAIN_RX.search(lead.get("name") or ""):
        s -= 20; why.append("крупная сеть/франшиза −20")
    if has_site and site.get("reachable") and site.get("ssl") and site.get("mobile") and \
       ((site.get("pagespeed") or 0) >= 70 or _MODERN_CMS.search(site.get("cms") or "")):
        s -= 40; why.append("современный сайт −40")
    if _WEBSTUDIO_RX.search((lead.get("name") or "") + " " + " ".join(lead.get("_rubrics") or [])):
        s -= 100; why.append("IT/веб-студия/конкурент −100")

    s = max(0, min(100, s))
    lead["score"] = s
    lead["status"] = "hot" if s > 60 else ("warm" if s >= 40 else "cold")
    lead["reasons"] = why
    lead["recommendation"] = _recommendation(lead)
    return s


def _recommendation(lead):
    site = lead.get("site") if isinstance(lead.get("site"), dict) else {}
    recs = []
    if not lead.get("website"):
        recs.append("лендинг")
    elif not site_checked(site):
        recs.append("проверить сайт")
    elif not site.get("reachable"):
        recs.append("новый сайт")
    elif not site.get("mobile"):
        recs.append("адаптивная вёрстка")
    if not site.get("has_booking") and re.search(
            r"(сервис|салон|клиник|барбер|студи|ремонт|мастер|фитнес)", (lead.get("industry") or "").lower()):
        recs.append("онлайн-запись")
    if not lead.get("website") and lead.get("socials"):
        recs.append("перенос из соцсетей")
    if (lead.get("rating") or 0) >= 4:
        recs.append("карточка в 2ГИС")
    return " + ".join(dict.fromkeys(recs)) or "аудит и редизайн"


# --------------------------------------------------------------------------
#  Анализ сайтов через настоящий стелс-браузер (antibot.py)
# --------------------------------------------------------------------------
def _pagespeed_score(url, pagespeed_key):
    """Оценка скорости Google PageSpeed (0–100) или None."""
    ps = source_json("pagespeed", "pagespeedonline/v5/runPagespeed",
                     {"url": url, "key": pagespeed_key,
                      "strategy": "mobile", "category": "performance"}, timeout=40)
    try:
        score = (((ps or {}).get("lighthouseResult") or {}).get("categories") or {}).get("performance", {}).get("score")
        return int(score * 100) if score is not None else None
    except Exception:
        return None


# Отказ самого PageSpeed (ключ, квота, API выключен) — дальше в этом прогоне
# стучаться бессмысленно.
_PS_FATAL = re.compile(r"(API key|api_key|quota|RESOURCE_EXHAUSTED|PERMISSION_DENIED|"
                       r"has not been used|is disabled|billing)", re.I)

# Ошибки Lighthouse, которые значат «сайт не открылся», а не «сломался сам PageSpeed».
_PS_UNREACHABLE = re.compile(r"(FAILED_DOCUMENT_REQUEST|ERRORED_DOCUMENT_REQUEST|DNS_FAILURE|"
                             r"NO_FCP|DOCUMENT_REQUEST|NOT_HTML|INSECURE_DOCUMENT_REQUEST)", re.I)


def analyze_by_pagespeed(url, pagespeed_key=""):
    """Проверка сайта без браузера — одним запросом к Google PageSpeed.

    Нужна там, где нет Playwright (в установщик он не входит). Google сам
    открывает сайт и говорит, открылся ли он, есть ли https, адаптив, скорость
    и SEO. Формы и CMS так не увидеть — они остаются None («не проверено»).
    Возвращает None, если PageSpeed не ответил по своей причине (ключ, квота):
    тогда сайт считается непроверенным, а не плохим."""
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    ps = source_json("pagespeed", "pagespeedonline/v5/runPagespeed",
                     {"url": url, "key": pagespeed_key, "strategy": "mobile",
                      "category": "performance"}, timeout=40)
    res = {"reachable": False, "final_url": None, "ssl": False, "mobile": None,
           "speed_ms": None, "pagespeed": None, "cms": None, "has_form": None,
           "has_booking": None, "seo": None, "cta": None, "error": None, "via": "pagespeed"}
    if not ps:
        return None
    err = (ps.get("error") or {}).get("message") or ""
    lr = ps.get("lighthouseResult") or {}
    rt = (lr.get("runtimeError") or {})
    if err or rt.get("code"):
        text = f"{err} {rt.get('code') or ''} {rt.get('message') or ''}"
        if _PS_UNREACHABLE.search(text):
            res["error"] = text.strip()[:120]
            return res
        if _PS_FATAL.search(err):
            global last_source_error
            source_down("pagespeed", err)
            last_source_error = err[:160]
        return None
    if not lr:
        return None
    final = lr.get("finalDisplayedUrl") or lr.get("finalUrl") or url
    audits = lr.get("audits") or {}

    def passed(aid):
        a = audits.get(aid)
        if not a or a.get("score") is None:
            return None
        return a.get("score") >= 0.9

    res["reachable"] = True
    res["final_url"] = final
    res["ssl"] = final.lower().startswith("https://")
    res["mobile"] = passed("viewport")
    si = (audits.get("speed-index") or {}).get("numericValue")
    res["speed_ms"] = int(si) if isinstance(si, (int, float)) else None
    score = ((lr.get("categories") or {}).get("performance") or {}).get("score")
    res["pagespeed"] = int(score * 100) if score is not None else None
    return res


def _analyze_by_pagespeed_all(targets, settings, store, log, stop):
    """Анализ сайтов только через PageSpeed — несколько запросов параллельно."""
    from concurrent.futures import ThreadPoolExecutor
    key = (settings.get("pagespeed_key") or "").strip()
    workers = max(1, min(4, int(settings.get("concurrency") or 3)))
    log(f"[site] браузера нет — проверяю {len(targets)} сайтов через Google PageSpeed "
        f"({workers} потока, до минуты на сайт)")
    done = {"n": 0, "ok": 0}

    def one(lead):
        if stop() or "pagespeed" in DEAD_SOURCES:
            return
        site = analyze_by_pagespeed(lead["website"], key)
        done["n"] += 1
        if site is None:
            return                     # PageSpeed не ответил — сайт остаётся непроверенным
        done["ok"] += 1
        site["_done"] = True
        lead["site"] = site
        compute_score(lead)
        store.upsert_one(lead)
        if done["n"] % 10 == 0:
            log(f"[site] проверено {done['n']}/{len(targets)}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, targets))
    if "pagespeed" in DEAD_SOURCES:
        log(f"[site] PageSpeed отказал и остановлен: {last_source_error or 'причина выше'}")
    log(f"[site] PageSpeed: проверено {done['ok']} из {len(targets)}")


async def _analyze_one(TB, page, url, pagespeed_key, use_ps=False):
    res = {"reachable": False, "final_url": None, "ssl": False, "mobile": False,
           "speed_ms": None, "pagespeed": None, "cms": None, "has_form": False,
           "has_booking": False, "seo": False, "cta": False, "error": None}
    if not url:
        return res
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    t0 = time.time()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        res["reachable"] = True
        res["speed_ms"] = int((time.time() - t0) * 1000)
        res["final_url"] = page.url
        res["ssl"] = (page.url or "").lower().startswith("https://")
        try:
            await TB.accept_cookies(page)
            if await TB.is_captcha_present(page):
                await TB.handle_captcha(page)
        except Exception:
            pass
        info = await page.evaluate(r"""() => {
          const q=s=>document.querySelector(s);
          const gen=(q('meta[name="generator"]')||{}).content||'';
          const html=document.documentElement.innerHTML.slice(0,20000);
          const cms=/wp-content|wp-json/i.test(html)?'WordPress':(/bitrix/i.test(html)?'1C-Bitrix':(/tilda/i.test(html)?'Tilda':(/cs=|wixstatic|_wix/i.test(html)?'Wix':(gen||''))));
          const bookingRx=/(онлайн[- ]?запис|записать\w*\s+онлайн|book(ing)?|забронир|запись на приём|записаться)/i;
          const text=(document.body?document.body.innerText:'').slice(0,8000);
          return {viewport:!!q('meta[name="viewport"]'),title:!!(document.title&&document.title.length>3),
            desc:!!q('meta[name="description"]'),form:!!q('form')||document.querySelectorAll('input,textarea').length>=2,
            booking:bookingRx.test(html)||bookingRx.test(text),
            cta:!!(q('a[href^="tel:"]')||q('a[href^="mailto:"]'))||/(заказать|связаться|оставить заявк|получить консультац)/i.test(text),
            cms:cms};
        }""")
        res["mobile"] = bool(info.get("viewport"))
        res["cms"] = info.get("cms") or None
        res["has_form"] = bool(info.get("form"))
        res["has_booking"] = bool(info.get("booking"))
        res["seo"] = bool(info.get("title") and info.get("desc"))
        res["cta"] = bool(info.get("cta"))
    except Exception as e:
        res["error"] = str(e)[:120]
        res["reachable"] = False
    if use_ps and res["reachable"]:
        res["pagespeed"] = _pagespeed_score(res["final_url"] or url, pagespeed_key)
    return res


async def _analyze_all(leads, settings, store, log, stop):
    targets = [l for l in leads if l.get("website") and not (l.get("site") or {}).get("_done")]
    if not targets:
        return
    pagespeed_key = (settings.get("pagespeed_key") or "").strip()
    use_ps = key_ready("pagespeed", pagespeed_key)
    try:
        import antibot as TB
        from playwright.async_api import async_playwright
    except Exception as e:
        # Браузера нет (в установщик он не входит) — проверяем через PageSpeed,
        # если есть ключ. Без ключа сайты остаются непроверенными.
        if use_ps:
            _analyze_by_pagespeed_all(targets, settings, store, log, stop)
        else:
            log(f"[site] анализ сайтов пропущен: нет браузера (Playwright) и ключа PageSpeed"
                f" ({key_missing('pagespeed')})")
        return
    log("[site] PageSpeed: " + ("оценка скорости включена" if use_ps
                                 else f"не используется — {key_missing('pagespeed')}"))
    headless = bool(settings.get("headless", True))
    proxies = [p for p in (settings.get("proxies") or []) if p] or [None]
    concurrency = max(1, int(settings.get("concurrency") or 3))
    log(f"[site] анализ {len(targets)} сайтов в {concurrency} вкладок…")
    import asyncio
    idx = {"i": 0}
    lock = asyncio.Lock()

    launched = {"n": 0}
    async with async_playwright() as pw:
        async def worker(wid):
            proxy = proxies[wid % len(proxies)]
            browser = None
            try:
                browser = await TB.launch_browser(pw, headless=headless, proxy=proxy)
                launched["n"] += 1
                context = await TB.new_context(browser, locale="ru-RU")
                page = await context.new_page()
                while not stop():
                    async with lock:
                        i = idx["i"]
                        if i >= len(targets):
                            break
                        idx["i"] = i + 1
                    lead = targets[i]
                    site = await _analyze_one(TB, page, lead["website"], pagespeed_key, use_ps)
                    site["_done"] = True
                    lead["site"] = site
                    compute_score(lead)
                    store.upsert_one(lead)
                    if (i + 1) % 10 == 0:
                        log(f"[site] проанализировано {i + 1}/{len(targets)}")
            except Exception as e:
                log(f"[site w{wid}] упал: {str(e)[:100]}")
            finally:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
        await asyncio.gather(*[worker(w) for w in range(concurrency)], return_exceptions=True)
    # Playwright стоит, но браузер к нему не скачан (или не запускается) —
    # тогда хотя бы PageSpeed, если есть ключ.
    if not launched["n"] and not stop():
        left = [l for l in targets if not (l.get("site") or {}).get("_done")]
        if use_ps and left:
            log("[site] браузер не запустился — переключаюсь на Google PageSpeed")
            _analyze_by_pagespeed_all(left, settings, store, log, stop)
        return
    log("[site] анализ сайтов завершён")


def analyze_existing(settings, store, log, stop_event):
    """Кнопка «Проверить сайты»: прогнать через проверку тех, у кого сайт есть,
    но ещё не проверялся. Сначала — у кого есть телефон (им можно звонить)."""
    import asyncio
    DEAD_SOURCES.discard("pagespeed")
    SOURCE_NOTES.pop("pagespeed", None)

    def stop():
        return stop_event.is_set()

    todo = [l for l in store.all() if not l.get("deleted") and l.get("website")
            and not site_checked(l.get("site"))]
    todo.sort(key=lambda l: (not str(l.get("phone") or "").strip(), -(l.get("score") or 0)))
    if not todo:
        log("[site] непроверенных сайтов нет — всё уже проверено")
        return {"checked": 0, "notes": {}}
    log(f"[site] проверка сайтов из базы: {len(todo)} (сначала с телефоном). Остановить — кнопкой ■")
    asyncio.run(_analyze_all(todo, settings, store, log, stop))
    done = sum(1 for l in todo if site_checked(l.get("site")))
    left = len(todo) - done
    log(f"[site] проверено {done} из {len(todo)}" + (f" · осталось {left}" if left else ""))
    return {"checked": done, "notes": dict(SOURCE_NOTES)}


# --------------------------------------------------------------------------
#  Оркестратор (синхронный — крутится в фоновом потоке сервера)
# --------------------------------------------------------------------------
def collect(settings, store, log, stop_event, progress=None):
    # какие источники включены («живая сеть»). По умолчанию 2ГИС + OSM (OSM — без ключа).
    enabled = settings.get("sources") or ["2gis", "osm"]
    if isinstance(enabled, str):
        enabled = [s.strip() for s in enabled.split(",") if s.strip()]
    enabled = [s.lower() for s in enabled]
    key = (settings.get("gis_api_key") or "").strip()
    DEAD_SOURCES.clear()
    SOURCE_NOTES.clear()
    for line in keys_summary():
        log(line)

    cities = [c.strip() for c in (settings.get("cities") or "").splitlines() if c.strip()]
    rub_filter = [w.strip().lower() for w in (settings.get("rubrics_filter") or "").split(",") if w.strip()]
    extra_excl = [w.strip() for w in (settings.get("exclude") or "").split(",") if w.strip()]
    if not cities:
        log("укажи хотя бы один город (собираем всех).")
        return {"added": 0, "blocked": False, "spent": 0}
    if not enabled:
        log("не выбран ни один источник (2ГИС / OSM / Яндекс / ВК) — включи в Settings.")
        return {"added": 0, "blocked": False, "spent": 0}
    if "2gis" in enabled and not key_ready("2gis", key):
        log(f"2ГИС включён, но {key_missing('2gis')} — пропускаю 2ГИС (OSM работает без ключа).")
        SOURCE_NOTES["2gis"] = key_missing("2gis")
        enabled = [s for s in enabled if s != "2gis"]
        if not enabled:
            return {"added": 0, "blocked": False, "spent": 0, "notes": dict(SOURCE_NOTES)}

    max_leads = int(settings.get("max_leads") or 2000)
    max_pages = int(settings.get("max_pages") or 10)
    analyze = bool(settings.get("analyze_sites", True))
    budget = Budget(int(settings.get("api_budget") or 800))   # бюджет ТОЛЬКО для 2ГИС
    probe = {}
    cache = _load_cache()
    keyhash = hashlib.sha1((key or "nokey").encode("utf-8")).hexdigest()[:8]
    import sources as SRC   # лениво: sources импортирует из leadfinder

    def stop():
        return stop_event.is_set()

    added = [0]
    got_any = [False]
    new_leads = []
    per_src = {}

    def push(lead):
        merged, is_new = store.upsert_one(lead)
        if not is_new:
            compute_score(merged)          # после склейки мог вернуться проверенный сайт
        if is_new:
            added[0] += 1
            new_leads.append(lead)
            per_src[lead.get("source") or "?"] = per_src.get(lead.get("source") or "?", 0) + 1
            if progress:
                progress()
        return added[0] < max_leads

    log(f"сбор лидов: города {cities}, источники {enabled}, лимит {max_leads}")
    for city in cities:
        if stop() or added[0] >= max_leads:
            break

        # ---------- 2ГИС ----------
        if "2gis" in enabled and not budget.blocked and budget.allow():
            ck = f"{city.strip().lower()}|{keyhash}"
            ent = cache.get(ck)
            if ent and (time.time() - ent.get("ts", 0) < 7 * 86400) and ent.get("region_id") and ent.get("rubrics"):
                region_id, rubrics = ent["region_id"], ent["rubrics"]
                log(f"[2gis] {city}: регион и рубрики из кэша ({len(rubrics)} рубрик) — 0 запросов")
            else:
                region_id = resolve_region(city, key, log, budget)
                rubrics = get_rubrics(region_id, key, log, budget) if region_id else []
                if region_id and rubrics:
                    cache[ck] = {"region_id": region_id, "rubrics": rubrics, "ts": time.time()}
                    _save_cache(cache)
            if region_id and rubrics:
                if rub_filter:
                    rubrics = [r for r in rubrics if any(f in (r["name"] or "").lower() for f in rub_filter)]
                seen_terms = set()
                for ri, r in enumerate(rubrics, 1):
                    if stop() or added[0] >= max_leads or not budget.allow():
                        break
                    items = collect_by_rubric(region_id, r, key, max_pages, log, stop, city,
                                              budget=budget, probe=probe, seen_terms=seen_terms)
                    if items:
                        got_any[0] = True
                    kept = 0
                    for it in items:
                        lead = parse_gis_item(it, city)
                        if is_excluded(lead, extra_excl):
                            continue
                        compute_score(lead)
                        if not push(lead):
                            break
                        kept += 1
                    if kept:
                        log(f"[2gis:{city}] {ri}/{len(rubrics)} «{r['name']}»: +{kept} · всего {added[0]}")

        # ---------- OSM / Яндекс / ВК ----------
        for src in ("osm", "yandex", "vk"):
            if src not in enabled or src in DEAD_SOURCES or stop() or added[0] >= max_leads:
                continue
            try:
                leads = SRC.REGISTRY[src](city, settings, log, stop, max_leads - added[0])
            except Exception as e:
                log(f"[{src}] источник упал: {str(e)[:120]}")
                leads = []
            kept = 0
            for lead in leads:
                if is_excluded(lead, extra_excl):
                    continue
                compute_score(lead)
                if not push(lead):
                    break
                kept += 1
            if kept:
                got_any[0] = True
                log(f"[{src}:{city}] +{kept} · всего {added[0]}")

    tail = f" · запросов 2ГИС: {budget.used}" + (f"/{budget.cap}" if budget.cap else "") if "2gis" in enabled else ""
    log(f"собрано организаций: {added[0]}{tail}")
    parts = []
    for src in dict.fromkeys(list(enabled) + list(SOURCE_NOTES)):
        name = SOURCE_NAMES.get(src, src)
        if src in SOURCE_NOTES:
            parts.append(f"{name} — остановлен: {SOURCE_NOTES[src].rstrip('. ')}")
        elif src in enabled:
            parts.append(f"{name} — +{per_src.get(src, 0)}")
    if parts:
        log("итог по источникам: " + " · ".join(parts))
    if added[0] == 0:
        if not got_any[0]:
            log("пусто: ни один источник не отдал организаций. Проверь ключи/город "
                "или включи OSM (он без ключа).")
        else:
            log("пусто после фильтров/исключений — ослабь фильтр/исключения.")
        return {"added": 0, "blocked": budget.blocked, "spent": budget.used, "notes": dict(SOURCE_NOTES)}

    if analyze and not stop():
        import asyncio
        try:
            asyncio.run(_analyze_all(new_leads, settings, store, log, stop))
        except Exception as e:
            log(f"[site] анализ прерван: {str(e)[:100]}")

    hot = sum(1 for l in store.all() if l.get("status") == "hot")
    warm = sum(1 for l in store.all() if l.get("status") == "warm")
    log(f"готово: в базе {len(store.all())} лидов (🔥hot {hot}, warm {warm})")
    return {"added": added[0], "blocked": budget.blocked, "spent": budget.used, "notes": dict(SOURCE_NOTES)}
