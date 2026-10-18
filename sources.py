"""Дополнительные источники лидов («живая сеть») помимо 2ГИС:

  · osm     — OpenStreetMap (Overpass API + Nominatim). БЕЗ ключа, без аккаунта, без жёсткой квоты.
  · yandex  — Яндекс.Карты (Search API «Поиск по организациям»). Нужен apikey (developer.tech.yandex.ru).
  · vk      — ВКонтакте (groups.search + groups.getById). Нужен access_token. Бизнесы с VK-страницей.

Каждый источник возвращает список лидов в общей схеме (leadfinder.blank_lead).
Дедуп между источниками делает store (по телефону/домену/названию), так что источники
можно включать вместе — получается объединённая «сеть».
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import leadfinder
from leadfinder import (_SSL_CTX, DEAD_SOURCES, Budget, blank_lead, http_json, key_missing, key_ready,
                        source_down, source_json)


def _lead(name, city, industry=None, address=None, phone=None, website=None, socials=None, source="?"):
    l = blank_lead()
    l["name"] = name
    l["city"] = city
    l["industry"] = industry
    l["address"] = address or ""
    l["phone"] = phone
    l["website"] = website
    l["socials"] = socials or []
    l["social_activity"] = "есть" if socials else None
    l["source"] = source
    l["_rubrics"] = [industry] if industry else []
    return l


# ==========================================================================
#  OSM — Overpass API + Nominatim (без ключа)
# ==========================================================================
def _nominatim_bbox(city, log):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": city, "format": "json", "limit": 1, "accept-language": "ru"})
    data = http_json(url, timeout=25)
    if not data:
        log("[osm] Nominatim: нет ответа (сеть/лимит)")
        return None
    try:
        bb = data[0]["boundingbox"]         # [south, north, west, east]
        return (float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3]))   # s,w,n,e
    except Exception:
        log(f"[osm] город '{city}' не найден в Nominatim")
        return None


_OSM_FILTERS = (
    'node["name"]["shop"]({b});way["name"]["shop"]({b});'
    'node["name"]["amenity"~"cafe|restaurant|bar|fast_food|pub|dentist|clinic|doctors|pharmacy|veterinary|car_wash|fuel|bank|cinema|driving_school"]({b});'
    'way["name"]["amenity"~"cafe|restaurant|bar|fast_food|pub|dentist|clinic|doctors|pharmacy|veterinary|car_wash|fuel|cinema"]({b});'
    'node["name"]["office"]({b});way["name"]["office"]({b});'
    'node["name"]["craft"]({b});way["name"]["craft"]({b});'
    'node["name"]["leisure"~"fitness_centre|sports_centre"]({b});way["name"]["leisure"~"fitness_centre|sports_centre"]({b});'
    'node["name"]["healthcare"]({b});way["name"]["healthcare"]({b});'
    'node["name"]["tourism"~"hotel|guest_house|hostel"]({b});'
)


# Публичный Overpass часто перегружен (429/504) — тогда пробуем зеркала, а в
# конце ещё раз основной. Запрос уходит POST: длинный запрос в адресе
# публичные серверы принимают хуже.
_OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)


def _overpass(ql, log, stop):
    body = urllib.parse.urlencode({"data": ql}).encode("utf-8")
    tries = _OVERPASS + _OVERPASS[:1]
    for i, url in enumerate(tries):
        if stop():
            return None
        if i == len(_OVERPASS):
            time.sleep(15)            # дать основному серверу передышку перед повтором
        host = urllib.parse.urlparse(url).netloc
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "BisquareLeadFinder/2.0"})
            # основной сервер честно считает большой запрос до ~70 с; зеркало,
            # которое молчит минуту, скорее всего лежит
            with urllib.request.urlopen(req, timeout=95 if i in (0, len(_OVERPASS)) else 60,
                                        context=_SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            why = {429: "перегружен (429)", 504: "перегружен (504)"}.get(e.code, f"HTTP {e.code}")
        except Exception as e:
            why = "не ответил" if "timed out" in str(e) else type(e).__name__
        nxt = "пробую зеркало" if i + 1 < len(_OVERPASS) else ("повторю основной" if i + 1 < len(tries) else "")
        log(f"[osm] {host}: {why}" + (f" — {nxt}" if nxt else ""))
    return None


def collect_osm(city, settings, log, stop, limit):
    box = _nominatim_bbox(city, log)
    if not box:
        return []           # город не найден или Nominatim молчит — следующий город может пройти
    bbox = "%s,%s,%s,%s" % box
    cap = min(int(limit) if limit else 3000, 5000)
    ql = "[out:json][timeout:70];(" + _OSM_FILTERS.replace("{b}", bbox) + ");out center tags " + str(cap) + ";"
    data = _overpass(ql, log, stop)
    if not data:
        log("[osm] Overpass: все серверы OSM сейчас перегружены — повтори позже "
            "(это общий бесплатный сервис, от нас не зависит)")
        if not stop():
            source_down("osm", "все серверы OSM перегружены — это общий бесплатный сервис, повтори позже")
        return []
    out = []
    for el in (data.get("elements") or []):
        if stop() or len(out) >= limit:
            break
        t = el.get("tags") or {}
        name = t.get("name")
        if not name:
            continue
        phone = t.get("phone") or t.get("contact:phone") or t.get("phone:mobile")
        site = t.get("website") or t.get("contact:website") or t.get("url")
        addr = ", ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        ind = (t.get("shop") or t.get("amenity") or t.get("office") or t.get("craft")
               or t.get("healthcare") or t.get("leisure") or t.get("tourism"))
        socials = [t[k] for k in ("contact:vk", "contact:instagram", "contact:facebook", "contact:telegram") if t.get(k)]
        out.append(_lead(name, city, ind, addr, phone, site, socials, "osm"))
    log(f"[osm] {city}: организаций {len(out)}")
    return out


# ==========================================================================
#  Яндекс.Карты — Search API (нужен apikey)
# ==========================================================================
_YA_CATS = [
    "кафе", "ресторан", "бар", "автосервис", "стоматология", "салон красоты", "барбершоп",
    "фитнес-клуб", "юридические услуги", "бухгалтерские услуги", "ремонт квартир", "строительная компания",
    "магазин", "пекарня", "кондитерская", "цветы", "ветеринарная клиника", "медицинский центр",
    "клининг", "автомойка", "шиномонтаж", "турагентство", "фотостудия", "типография", "мебель",
    "натяжные потолки", "пластиковые окна", "автошкола", "детский центр", "языковая школа", "маникюр",
]


def collect_yandex(city, settings, log, stop, limit):
    key = (settings.get("yandex_api_key") or "").strip()
    if not key_ready("yandex", key):
        log(f"[yandex] {key_missing('yandex')} (Yandex Search API) — источник пропущен")
        source_down("yandex", key_missing("yandex"))
        return []
    budget = Budget(int(settings.get("yandex_budget") or 400))
    out, seen = [], set()
    for cat in _YA_CATS:
        if stop() or len(out) >= limit or budget.blocked or not budget.allow():
            break
        for skip in (0, 50, 100, 150, 200):
            if stop() or len(out) >= limit or not budget.allow():
                break
            budget.hit()
            data = source_json("yandex", "v1/", {
                "apikey": key, "text": f"{cat} {city}", "lang": "ru_RU",
                "type": "biz", "results": 50, "skip": skip}, log=log)
            if data is None:
                log("[yandex] нет ответа/ошибка (403 = неверный ключ или исчерпан суточный лимит) — стоп источника")
                source_down("yandex", leadfinder.last_source_error
                            or "не отвечает — неверный ключ или кончился суточный лимит")
                budget.blocked = True
                break
            feats = data.get("features") or []
            if not feats:
                break
            for f in feats:
                if len(out) >= limit:
                    break
                pr = ((f.get("properties") or {}).get("CompanyMetaData")) or {}
                name = pr.get("name")
                if not name:
                    continue
                oid = pr.get("id") or name
                if oid in seen:
                    continue
                seen.add(oid)
                phones = [p.get("formatted") for p in (pr.get("Phones") or []) if p.get("formatted")]
                cats = [c.get("name") for c in (pr.get("Categories") or []) if c.get("name")]
                out.append(_lead(name, city, cats[0] if cats else cat, pr.get("address"),
                                 phones[0] if phones else None, pr.get("url"), None, "yandex"))
            if len(feats) < 50:
                break
    log(f"[yandex] {city}: организаций {len(out)} (запросов {budget.used})")
    return out


# ==========================================================================
#  ВКонтакте — groups.search + groups.getById (нужен access_token)
# ==========================================================================
_VK_CATS = ["бизнес", "услуги", "магазин", "кафе", "салон", "сервис", "студия", "клиника", "ремонт", "доставка", "мастерская"]


# Частые отказы ВК — человеческим языком для итога прогона.
_VK_ERRORS = {
    5:  "токен недействителен (истёк или отозван) — нужен новый",
    6:  "слишком много запросов в секунду — повтори позже",
    29: "кончился дневной лимит запросов ВК — завтра заработает",
    27: "токен сообщества не подходит — нужен токен пользователя",
    28: "сервисный ключ не подходит — нужен токен пользователя",
}


def collect_vk(city, settings, log, stop, limit):
    token = (settings.get("vk_token") or "").strip()
    if not key_ready("vk", token):
        log(f"[vk] {key_missing('vk')} (токен VK API) — источник пропущен")
        source_down("vk", key_missing("vk"))
        return []
    budget = Budget(int(settings.get("vk_budget") or 200))
    ids, seen = [], set()
    for cat in _VK_CATS:
        if stop() or not budget.allow():
            break
        budget.hit()
        data = source_json("vk", "method/groups.search", {
            "q": f"{cat} {city}", "type": "group", "count": 100,
            "access_token": token, "v": "5.199"}, log=log)
        if not data:
            if "vk" in DEAD_SOURCES:
                break
            continue
        if data.get("error"):
            err = data["error"] or {}
            log(f"[vk] ошибка API: {err.get('error_msg')} — стоп источника")
            source_down("vk", _VK_ERRORS.get(err.get("error_code"), f"ошибка API: {err.get('error_msg')}"))
            break
        for g in ((data.get("response") or {}).get("items") or []):
            gid = g.get("id")
            if gid and gid not in seen:
                seen.add(gid)
                ids.append(gid)
    out = []
    for i in range(0, len(ids), 25):
        if stop() or len(out) >= limit or not budget.allow():
            break
        budget.hit()
        data = source_json("vk", "method/groups.getById", {
            "group_ids": ",".join(str(x) for x in ids[i:i + 25]),
            "fields": "contacts,site,city,activity,description",
            "access_token": token, "v": "5.199"}, log=log)
        if not data or data.get("error"):
            continue
        resp = data.get("response")
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        for g in (groups or []):
            name = g.get("name")
            if not name:
                continue
            phone = None
            for c in (g.get("contacts") or []):
                if c.get("phone"):
                    phone = c["phone"]
                    break
            city_name = (g.get("city") or {}).get("title") if isinstance(g.get("city"), dict) else city
            out.append(_lead(name, city, g.get("activity"), city_name, phone, g.get("site"),
                             [f"vk.com/club{g.get('id')}"], "vk"))
    log(f"[vk] {city}: сообществ {len(out)} (запросов {budget.used})")
    return out


REGISTRY = {"osm": collect_osm, "yandex": collect_yandex, "vk": collect_vk}
