"""Минимальный срок аренды: из полей сайта и из текста объявления.

lease_term(row) → {"kind": …, "months": N | None, "frag": "цитата/поле"} или None (о сроке ничего нет).
kind:
  "long"  — явный минимум от 3 мес («Срок минимальной аренды: от 6 месяцев», «от года», minRentPeriod Яндекса,
            штраф/залог не возвращается при выезде раньше N мес);
  "soft"  — «на длительный срок», «долгосрочно», ЦИАН «длительно»: числа нет, договориться на 2 мес — вряд ли;
  "short" — явно можно коротко: «на короткий срок — 1,2,3 месяца», «от 1 месяца», ЦИАН «несколько месяцев».
Явный минимум важнее «можно коротко», «можно коротко» важнее «длительного срока».
"""
import re

SHORT_MAX = 2          # ищем на 2 месяца: минимум до 2 мес включительно подходит

_NUM = {"одного": 1, "один": 1, "1-го": 1, "двух": 2, "два": 2, "трёх": 3, "трех": 3, "три": 3, "четырех": 4,
        "четырёх": 4, "пяти": 5, "шести": 6, "полугода": 6, "года": 12, "год": 12, "одного года": 12}
_N = r"(\d{1,2}|одного|двух|трёх|трех|четырех|четырёх|пяти|шести)"
# «от 6 месяцев», «не менее 11 мес», «минимум 3 месяца», «от полугода», «от года», «на год и более»
_MIN_RX = re.compile(
    rf"(?:\bот|не\s+менее|не\s+меньше|минимум|минимально|min\.?)\s*(?:на\s+)?(?:{_N}\s*-?\s*(?:х|ти|ми|и)?\s*(мес(?:яц\w*|\.|\b))"
    r"|(полугода|года|1\s*года|одного\s+года)\b)"
    r"|\bна\s+(?:срок\s+)?(?:(\d{1,2})\s*(?:-?\s*ти|-?\s*и)?\s*мес(?:яц\w*|\.|\b)|(год|1\s*год)\b)(?:\s+и\s+более|\s+и\s+больше)?"
    r"|\bна\s+(полгода)\b", re.I)
# что считается контекстом срока аренды (а не рассрочки залога, кешбэка и т. п.)
_CTX_RX = re.compile(r"срок|аренд|сда[её]т|сдаю|сдам|сдаем|сдаём|сдаётся|сдается|проживан|заселен|договор", re.I)
_STOP_RX = re.compile(r"рассроч|разб|раздел|частям|кешб|кэшб|залог|депозит|оплат|плат[её]ж|комисс|предоплат|"
                      r"страхов|в\s+месяц|ежемесяч|аванс|за\s+последн|посуточ|дом\w*\s+постро|сдан\w*\s+в\s+\d{4}", re.I)
# «при выезде/расторжении ранее 6 месяцев … депозит не возвращается»
_PENALTY_RX = re.compile(rf"(?:раньше|ранее|менее|меньше)\s+(?:чем\s+)?(?:через\s+)?{_N}\s*(?:-?х\s*)?мес", re.I)
_PENALTY_CTX = re.compile(r"расторж|выезд|съезд|выселен|досрочн", re.I)
_SHORT_RX = re.compile(
    r"на\s+короткий\s+срок|краткосрочн|среднесрочн|на\s+несколько\s+месяц|от\s+нескольких\s+месяц|"
    r"от\s+(?:1|одного)\s*(?:-?го\s*)?мес(?:яц|\.|\b)|от\s+месяца|помесячн\w*\s+(?:аренд|сда)|на\s+сессию|"
    r"на\s+(?:1|2|один|два)(?:\s*[-–,]\s*\d)*\s*мес(?:яц|\.|\b)|до\s+(?:лета|весны|мая|июня|нового\s+года)", re.I)
_NEG_AFTER = re.compile(r"^.{0,50}?(?:не\s+рассматр|не\s+сда|не\s+интерес|не\s+беспоко|не\s+занима|не\s+пиш|исключ|нельзя|не\s+предлаг|разгруз)", re.I | re.S)
_NEG_BEFORE = re.compile(r"(?:\bне|\bбез|\bкроме)\s+(?:\w+\s+){0,3}$", re.I)
_SOFT_RX = re.compile(r"длительн\w*\s+(?:срок|период|аренд|проживан|основ)|на\s+длительное|долгосроч|долгий\s+срок|"
                      r"на\s+долгосрок|надолго|на\s+долгое\s+время|длительно\b", re.I)


def _months(m):
    for g in m.groups():
        if not g:
            continue
        g = re.sub(r"\s+", " ", g.lower())
        if g.isdigit():
            return int(g)
        if g in ("полгода", "полугода"):
            return 6
        if g in ("год", "года", "1 года", "1 год", "одного года"):
            return 12
        if g in _NUM:
            return _NUM[g]
    return None


def _frag(text, a, b, pad=35):
    s = max(0, a - pad)
    e = min(len(text), b + pad)
    return ("…" if s else "") + re.sub(r"\s+", " ", text[s:e]).strip() + ("…" if e < len(text) else "")


def from_text(text):
    text = text if isinstance(text, str) else ""
    longs, shorts, softs = [], [], []
    for m in _MIN_RX.finditer(text):
        n = _months(m)
        if not n or n > 36:
            continue
        before = text[max(0, m.start() - 45):m.start()]
        if _STOP_RX.search(before + m.group(0)) or _STOP_RX.match(text[m.end():m.end() + 12]):
            continue
        # «на 6 месяцев» без слов про срок/аренду рядом — скорее не о сроке
        if m.group(0).lower().startswith("на") and not _CTX_RX.search(text[max(0, m.start() - 60):m.end() + 20]):
            continue
        (shorts if n <= SHORT_MAX else longs).append((n, m.start(), m.end()))
    for m in _PENALTY_RX.finditer(text):
        n = _months(m)
        if n and n > SHORT_MAX and _PENALTY_CTX.search(text[max(0, m.start() - 120):m.end() + 120]):
            longs.append((n, m.start(), m.end()))
    for m in _SHORT_RX.finditer(text):
        if _STOP_RX.search(text[max(0, m.start() - 45):m.start()]):
            continue                                     # «залог можно разбить на 2 месяца»
        if _NEG_AFTER.match(text[m.end():m.end() + 60]) or _NEG_BEFORE.search(text[max(0, m.start() - 25):m.start()]):
            softs.append((None, m.start(), m.end()))     # «краткосрочную аренду не рассматриваем»
        else:
            shorts.append((None, m.start(), m.end()))
    for m in _SOFT_RX.finditer(text):
        softs.append((None, m.start(), m.end()))
    if longs:
        n, a, b = max(longs, key=lambda x: x[0])
        return {"kind": "long", "months": n, "frag": _frag(text, a, b)}
    if shorts:
        n, a, b = shorts[0]
        return {"kind": "short", "months": n, "frag": _frag(text, a, b)}
    if softs:
        _, a, b = softs[0]
        return {"kind": "soft", "months": None, "frag": _frag(text, a, b)}
    return None


_YA_MIN = re.compile(r'"minRentPeriod":\s*(\d+)')
_YA_PEN = re.compile(r'"prematureRentTerminationPenalty":\s*(\d+)')
_CIAN = re.compile(r'"leaseTermType":\s*"(\w+)"')


def lease_term(r):
    """Срок по объявлению (строка базы): поля сайта + текст. Явное из текста важнее поля «длительно»."""
    raw = r.get("raw_json")
    raw = raw if isinstance(raw, str) else ""
    t = from_text(r.get("description"))
    if t and t["kind"] == "long":
        return t
    badges = r.get("badges")
    m = _YA_MIN.search(raw) or re.search(r"(?:^| · )срок от (\d+) мес", badges if isinstance(badges, str) else "")
    if m and int(m.group(1)) > SHORT_MAX:
        pen = _YA_PEN.search(raw)
        note = f" · штраф за досрочный выезд {int(pen.group(1)):,} ₽".replace(",", " ") if pen and int(pen.group(1)) else ""
        return {"kind": "long", "months": int(m.group(1)), "frag": f"в карточке: минимальный срок {m.group(1)} мес{note}"}
    if m:
        return {"kind": "short", "months": int(m.group(1)), "frag": f"в карточке: минимальный срок {m.group(1)} мес"}
    if t:
        return t
    c = _CIAN.search(raw)
    if c and c.group(1) == "fewMonths":
        return {"kind": "short", "months": None, "frag": "в карточке ЦИАН: срок «несколько месяцев»"}
    if c and c.group(1) == "longTerm":
        return {"kind": "soft", "months": None, "frag": "в карточке ЦИАН: срок «длительно»"}
    return None
