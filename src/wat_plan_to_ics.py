#!/usr/bin/env python3
"""
WAT WEL: plan zajęć (HTML) -> kalendarz ICS do subskrypcji na iPhonie.

Pobiera stronę planu (np. plan prowadzącego dr. inż. Dominika Małego), parsuje
tabelę (daty w kolumnach, bloki godzinowe w wierszach, colspan/rowspan)
i zapisuje plik .ics z jawną strefą Europe/Warsaw.

Użycie:
    python src/wat_plan_to_ics.py                              # domyślny plan, oba semestry
    python src/wat_plan_to_ics.py --plan dr_inz._Maly_Dominik --semesters zima
    python src/wat_plan_to_ics.py --from-file zapisana_strona.htm --out docs/plan.ics

Zależności: requests, beautifulsoup4 (Python 3.10+).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

BASE_URL = "https://wel.wat.edu.pl/planyzajec/{semester}/{plan}.htm"
DEFAULT_PLAN = "dr_inz._Maly_Dominik"   # ZWERYFIKUJ z adresem strony
DEFAULT_OUT = "docs/plan.ics"
TZ = "Europe/Warsaw"

# Serwer WEL zwraca 403 dla żądań bez przeglądarkowego User-Agenta.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

# Siatka godzinowa WEL (bloki 2x45 min). ZWERYFIKUJ z oficjalną siatką WEL.
TIME_MAP = {
    "1-2": ("08:00", "09:35"),
    "3-4": ("09:50", "11:25"),
    "5-6": ("11:40", "13:15"),
    "7-8": ("13:30", "15:05"),
    "9-10": ("16:00", "17:35"),
    "11-12": ("17:50", "19:25"),
    "13-14": ("19:40", "21:15"),
}

TYPE_NAMES = {
    "w": "wykład", "ć": "ćwiczenia", "L": "laboratorium", "S": "seminarium",
    "P": "projekt", "E": "egzamin", "Ep": "egzamin poprawkowy", "Z": "zaliczenie",
    "Zp.": "zaliczenie poprawkowe", "Rep": "repetytorium", "r": "rezerwa", "i": "inne",
}
SKIP_TEXT = {"SSW"}  # samodzielna praca studenta – nie są to zajęcia prowadzącego
SPECIAL_NAMES = {"REZ": "Rezerwacja terminu (REZ)"}

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
         "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
WEEKDAYS = ("pon", "wt", "śr", "czw", "pt", "sob", "niedz")

SLOT_RE = re.compile(r"^\s*(\d{1,2}-\d{1,2})\s*$")
DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([IVX]+)\s*$")
SHARE_RE = re.compile(r"^\d+%$")
# Kody grup WAT, np. WEL25EQ1S1, IOE25WX1S1, WME23BE1S1 (lista rozdzielona ';' lub ',').
GROUP_LINE_RE = re.compile(r"^[A-Z]{2,4}\d{2}[A-Z0-9]{2,}(\s*[;,]\s*[A-Z]{2,4}\d{2}[A-Z0-9]{2,})*$")
TEACHER_RE = re.compile(r"\b(dr|mgr|prof|inż|kpt|ppłk|płk|mjr|por|ppor|chor|kmdr)\b", re.I)


# ---------------------------------------------------------------------------
# Pobieranie i dekodowanie
# ---------------------------------------------------------------------------

def decode_html(raw: bytes) -> str:
    """Serwer nie podaje charsetu w nagłówku – bierzemy go z <meta>."""
    head = raw[:4096].decode("ascii", errors="ignore")
    m = re.search(r'charset\s*=\s*["\']?([\w-]+)', head, re.I)
    enc = m.group(1).lower() if m else "utf-8"
    try:
        return raw.decode(enc)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def fetch(url: str) -> str | None:
    """Zwraca HTML albo None, jeśli strona nie istnieje (np. plan letni jeszcze nieopublikowany)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return decode_html(resp.content)


# ---------------------------------------------------------------------------
# Parsowanie
# ---------------------------------------------------------------------------

@dataclass
class Cell:
    text: str
    colspan: int = 1
    rowspan: int = 1


@dataclass
class Event:
    start: datetime
    end: datetime
    subject: str
    kind: str = ""
    room: str = ""
    groups: str = ""
    teacher: str = ""
    share: str = ""
    full_name: str = ""
    extra: list[str] = field(default_factory=list)


def page_meta(soup: BeautifulSoup) -> dict:
    text = " ".join(soup.get_text(" ").split())
    meta: dict = {}
    if m := re.search(r"ROK\s+AKADEMICKI\s+(\d{4})\s*/\s*(\d{4})", text, re.I):
        meta["year_start"] = int(m.group(1))
    elif m := re.search(r"semestrze\s+\w+\s+(\d{4})\s*/\s*(\d{4})", text, re.I):
        meta["year_start"] = int(m.group(1))   # plan prowadzącego: "w semestrze ZIMOWYM 2026/2027"
    if m := re.search(r"Data\s+aktualizacji:\s*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)", text):
        meta["updated"] = m.group(1)
    if m := re.search(r"SEMESTR\w*\s+(ZIMOW|LETN)", text, re.I):
        meta["semester_label"] = m.group(1).lower()
    return meta


def build_grid(soup: BeautifulSoup) -> dict[int, dict[int, Cell]]:
    """Logiczna siatka tabeli z rozwiniętymi colspan/rowspan (ta sama komórka pod wieloma kluczami)."""
    table = max(soup.find_all("table"), key=lambda t: len(t.find_all("tr")), default=None)
    if table is None:
        raise ValueError("Brak tabeli na stronie")
    grid: dict[int, dict[int, Cell]] = {}
    occupied: set[tuple[int, int]] = set()
    for r, tr in enumerate(table.find_all("tr")):
        c = 0
        for td in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in occupied:
                c += 1
            cell = Cell(
                text=td.get_text("\n", strip=True).replace("\xa0", " "),
                colspan=int(td.get("colspan", 1) or 1),
                rowspan=int(td.get("rowspan", 1) or 1),
            )
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied.add((r + dr, c + dc))
                    grid.setdefault(r + dr, {}).setdefault(c + dc, cell)
            c += cell.colspan
    return grid


def year_for(month: int, year_start: int) -> int:
    return year_start if month >= 8 else year_start + 1


def slot_of(row: dict[int, Cell]) -> str | None:
    for col in sorted(row)[:3]:
        if m := SLOT_RE.match(row[col].text):
            if m.group(1) in TIME_MAP:
                return m.group(1)
    return None


def subject_legend(grid, last_date_col: int) -> dict[str, str]:
    """Legenda 'skrót -> pełna nazwa' znajduje się na prawo od kolumn z datami."""
    legend: dict[str, str] = {}
    for r in sorted(grid):
        right = [grid[r][c] for c in sorted(grid[r]) if c > last_date_col]
        if len(right) >= 2:
            code, name = right[0].text.strip(), right[1].text.strip()
            if code and name and "\n" not in code and len(code) <= 12 \
                    and len(name) > len(code) and code not in TYPE_NAMES \
                    and not re.match(r"^\S+\s+\d+$", code):
                legend.setdefault(code, name)
    return legend


def parse_cell(text: str) -> dict:
    """Dwa układy komórek generatora Plansoft:
    - plan prowadzącego: GRUPA(Y) / SKRÓT / typ / sala(e)
    - plan grupy:        [50%] / SKRÓT / typ / sala(e) / podgrupa
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    share = ""
    while lines and SHARE_RE.match(lines[0]):
        share = lines.pop(0)
    groups = ""
    if lines and GROUP_LINE_RE.match(lines[0]):
        groups = re.sub(r"\s*;\s*", ", ", lines.pop(0))
    out = {"subject": lines[0] if lines else "", "kind": "", "room": "",
           "groups": groups, "teacher": "", "share": share, "extra": []}
    rest = lines[1:]
    if rest and rest[0] in TYPE_NAMES:
        out["kind"] = rest.pop(0)
    for line in rest:
        if TEACHER_RE.search(line):
            out["teacher"] = line
        elif not out["room"]:
            out["room"] = line
        elif not out["groups"]:
            out["groups"] = line
        else:
            out["extra"].append(line)
    return out


def parse_schedule(html: str) -> tuple[list[Event], dict]:
    soup = BeautifulSoup(html, "html.parser")
    meta = page_meta(soup)
    year_start = meta.get("year_start") or (
        datetime.now().year if datetime.now().month >= 8 else datetime.now().year - 1)
    grid = build_grid(soup)
    rows = sorted(grid)

    # Wiersze nagłówkowe: pierwsza komórka = dzień tygodnia, dalej daty "05 X".
    date_rows: list[tuple[int, dict[int, datetime]]] = []
    for r in rows:
        row = grid[r]
        first = row[min(row)].text.strip().lower()
        if not first.startswith(WEEKDAYS):
            continue
        cols = {}
        for c, cell in row.items():
            if m := DATE_RE.match(cell.text):
                month = ROMAN.get(m.group(2))
                if month:
                    try:
                        cols[c] = datetime(year_for(month, year_start), month, int(m.group(1)))
                    except ValueError:
                        pass
        if cols:
            date_rows.append((r, cols))
    if not date_rows:
        raise ValueError("Nie znaleziono wierszy z datami – zmienił się format strony?")

    last_date_col = max(c for _, cols in date_rows for c in cols)
    legend = subject_legend(grid, last_date_col)

    events: list[Event] = []
    seen: set[tuple[int, int]] = set()
    for i, (hdr, cols) in enumerate(date_rows):
        end_row = date_rows[i + 1][0] if i + 1 < len(date_rows) else rows[-1] + 1
        for r in range(hdr + 1, end_row):
            row = grid.get(r, {})
            slot = slot_of(row)
            if not slot:
                continue
            for c, day in cols.items():
                cell = row.get(c)
                if not cell or not cell.text.strip():
                    continue
                if (id(cell), c) in seen:   # rowspan: ta sama komórka w kolejnym bloku
                    continue
                seen.add((id(cell), c))
                if any(k in cell.text.split("\n") for k in SKIP_TEXT):
                    continue
                s_h, s_m = map(int, TIME_MAP[slot][0].split(":"))
                end_slot = slot
                if cell.rowspan > 1:
                    end_slot = slot_of(grid.get(r + cell.rowspan - 1, {})) or slot
                e_h, e_m = map(int, TIME_MAP[end_slot][1].split(":"))
                p = parse_cell(cell.text)
                events.append(Event(
                    start=day.replace(hour=s_h, minute=s_m),
                    end=day.replace(hour=e_h, minute=e_m),
                    subject=p["subject"], kind=p["kind"], room=p["room"],
                    groups=p["groups"], teacher=p["teacher"], share=p["share"],
                    full_name=legend.get(p["subject"], SPECIAL_NAMES.get(p["subject"], "")), extra=p["extra"],
                ))
    events.sort(key=lambda e: (e.start, e.subject))
    return events, meta


# ---------------------------------------------------------------------------
# ICS (RFC 5545)
# ---------------------------------------------------------------------------

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Warsaw
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE""".split("\n")


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


ROOM_RE = re.compile(r"^(\S+)\s+(\d+[A-Z]?|K|S)$")


def pretty_room(room: str) -> str:
    """'0.09 42' -> 's. 0.09, bud. 42'; nietypowe wpisy (ONLINE, AW 4) zostają bez zmian."""
    parts = []
    for r in (x.strip() for x in room.split(";") if x.strip()):
        m = ROOM_RE.match(r)
        parts.append(f"s. {m.group(1)}, bud. {m.group(2)}" if m and m.group(1) not in ("AW", "ST.nr") else r)
    return "; ".join(parts)


def summary_of(e: Event) -> str:
    """Na iPhonie widać głównie tytuł: przedmiot + typ + grupa + sala."""
    parts = [e.subject + (f" ({e.kind})" if e.kind else "")]
    if e.groups:
        parts.append(e.groups)
    if e.room:
        parts.append(pretty_room(e.room))
    return " · ".join(parts)


def description_of(e: Event, source: str) -> str:
    lines = []
    if e.full_name:
        lines.append(e.full_name)
    if e.kind:
        lines.append(f"Typ: {TYPE_NAMES.get(e.kind, e.kind)}")
    if e.groups:
        lines.append(f"Grupa: {e.groups}")
    if e.room:
        lines.append(f"Sala: {pretty_room(e.room)} (w planie: {e.room})")
    if e.share:
        lines.append(f"Udział grupy: {e.share}")
    if e.teacher:
        lines.append(f"Prowadzący: {e.teacher}")
    lines += e.extra
    lines.append(f"Źródło: {source}")
    return "\n".join(lines)


def make_uid(e: Event, counter: dict) -> str:
    # UID nie zależy od sali ani kolumny: zmiana sali = aktualizacja tego samego wydarzenia.
    key = f"{e.start:%Y%m%dT%H%M}|{e.subject}|{e.kind}|{e.groups}"
    n = counter.get(key, 0)
    counter[key] = n + 1
    h = hashlib.sha1(f"{key}|{n}".encode()).hexdigest()[:12]
    return f"{e.start:%Y%m%d}-{h}@wel-plan"


def dtstamp(meta: dict) -> str:
    """DTSTAMP = data aktualizacji planu, więc bez zmian w planie plik jest identyczny."""
    raw = meta.get("updated")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            local = datetime.strptime(raw or "", fmt).replace(tzinfo=ZoneInfo(TZ))
            return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            continue
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: list[Event], meta: dict, name: str, source: str,
              alarm_minutes: int = 0) -> str:
    stamp = dtstamp(meta)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WEL WAT plan -> ICS//PL",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        fold(f"X-WR-CALNAME:{esc(name)}"),
        fold(f"X-WR-CALDESC:{esc('Źródło: ' + source + ' | aktualizacja: ' + meta.get('updated', '?'))}"),
        f"X-WR-TIMEZONE:{TZ}",
        "X-PUBLISHED-TTL:P1D",           # sugestia częstotliwości odświeżania dla klientów
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        *VTIMEZONE,
    ]
    counter: dict = {}
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{make_uid(e, counter)}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={TZ}:{e.start:%Y%m%dT%H%M%S}",
            f"DTEND;TZID={TZ}:{e.end:%Y%m%dT%H%M%S}",
            fold(f"SUMMARY:{esc(summary_of(e))}"),
        ]
        if e.room:
            lines.append(fold(f"LOCATION:{esc(pretty_room(e.room))}"))
        lines.append(fold(f"DESCRIPTION:{esc(description_of(e, source))}"))
        lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE"]
        if alarm_minutes > 0:   # iOS: w ustawieniach subskrypcji wyłącz „Usuń alarmy”
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", fold(f"DESCRIPTION:{esc(summary_of(e))}"),
                      f"TRIGGER:-PT{alarm_minutes}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="WEL WAT plan (HTML) -> ICS")
    ap.add_argument("--plan", default=DEFAULT_PLAN, help="nazwa pliku planu bez .htm")
    ap.add_argument("--semesters", nargs="+", default=["zima", "lato"], choices=["zima", "lato"])
    ap.add_argument("--from-file", nargs="+", help="lokalne pliki HTML zamiast pobierania")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--name", default="WAT – plan zajęć (D. Mały)")
    ap.add_argument("--min-events", type=int, default=1,
                    help="bezpiecznik: nie nadpisuj pliku, jeśli wyszło mniej wydarzeń")
    ap.add_argument("--alarm-minutes", type=int, default=0,
                    help="przypomnienie X minut przed zajęciami (0 = brak)")
    args = ap.parse_args()

    sources: list[tuple[str, str]] = []
    if args.from_file:
        for p in args.from_file:
            sources.append((p, decode_html(Path(p).read_bytes())))
    else:
        for sem in args.semesters:
            url = BASE_URL.format(semester=sem, plan=args.plan)
            html = fetch(url)
            print(f"{'OK ' if html else '404'} {url}")
            if html:
                sources.append((url, html))

    all_events: list[Event] = []
    meta: dict = {}
    for src, html in sources:
        events, m = parse_schedule(html)
        print(f"  {src}: {len(events)} wydarzeń, aktualizacja planu: {m.get('updated', '?')}")
        all_events += events
        if dtstamp(m) >= dtstamp(meta) or not meta:
            meta = m

    if len(all_events) < args.min_events:
        print(f"BŁĄD: tylko {len(all_events)} wydarzeń – nie nadpisuję {args.out}", file=sys.stderr)
        return 1

    all_events.sort(key=lambda e: (e.start, e.subject))
    source = BASE_URL.format(semester="{zima|lato}", plan=args.plan) if not args.from_file else "plik lokalny"
    ics = build_ics(all_events, meta, args.name, source, args.alarm_minutes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ics, encoding="utf-8", newline="")
    print(f"Zapisano {out} ({len(all_events)} wydarzeń)")
    for e in all_events[:5]:
        print(f"  {e.start:%a %d.%m %H:%M}-{e.end:%H:%M} | {summary_of(e)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
