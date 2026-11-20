#!/usr/bin/env python3
"""Render AsrtMe's public grimoire panels; Python standard library only.

Normal runs use GitHub GraphQL with the workflow's GITHUB_TOKEN.  The optional
--public-snapshot mode bootstraps a review copy from public REST endpoints and
GitHub's public contribution calendar, without credentials or external writes.
No API failure in normal mode is converted into zero activity.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
PALETTE = ["#1c1014", "#5c1623", "#a52538", "#e14251", "#ff8279"]
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


class StatsError(RuntimeError):
    """A recoverable data error; leave the last successful panels in place."""


def valid_login(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", value):
        raise StatsError("Invalid GitHub login.")
    return value


def number(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise StatsError(f"GitHub returned an invalid {name}.")
    return value


def period(now: datetime) -> tuple[date, date]:
    end = now.astimezone(timezone.utc).date()
    return end - timedelta(days=364), end


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def fetch_bytes(url: str, *, token: str | None = None, payload: dict | None = None) -> bytes:
    headers = {
        "User-Agent": "AsrtMe-grimoire-stats/1.0",
        "Accept": "application/vnd.github+json" if url.startswith(API) else "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        # Do not echo server bodies, request headers, or credentials.
        raise StatsError(f"GitHub HTTP {exc.code}; previous panels were kept.") from None
    except (URLError, TimeoutError, OSError):
        raise StatsError("GitHub could not be reached; previous panels were kept.") from None


def fetch_json(url: str, *, token: str | None = None, payload: dict | None = None):
    try:
        return json.loads(fetch_bytes(url, token=token, payload=payload))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StatsError("GitHub returned invalid JSON; previous panels were kept.") from None


def graphql(query: str, variables: dict, token: str) -> dict:
    result = fetch_json(API + "/graphql", token=token,
                        payload={"query": query, "variables": variables})
    if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
        raise StatsError("GitHub GraphQL returned errors; previous panels were kept.")
    user = result["data"].get("user")
    if not isinstance(user, dict):
        raise StatsError("GitHub user data was not available.")
    if str(user.get("login", "")).casefold() != variables["login"].casefold():
        raise StatsError("GitHub returned data for an unexpected user.")
    return user


def select_repositories(nodes: list[dict], login: str, *, rest: bool = False) -> list[tuple[str, int]]:
    """Owned, public, non-fork repositories, excluding the profile repository.

    Archived public projects remain included.  Repository names are used only
    while counting and are not written to the public snapshot.
    """
    selected = []
    for node in nodes:
        if not isinstance(node, dict):
            raise StatsError("GitHub returned an invalid repository entry.")
        owner = node.get("owner") or {}
        private_key, fork_key = ("private", "fork") if rest else ("isPrivate", "isFork")
        if not isinstance(owner, dict) or not isinstance(node.get(private_key), bool) or not isinstance(node.get(fork_key), bool):
            raise StatsError("GitHub returned incomplete repository visibility data.")
        name = str(node.get("full_name" if rest else "nameWithOwner", ""))
        if not name or "/" not in name:
            raise StatsError("GitHub returned an invalid repository name.")
        if owner.get("login", "").casefold() != login.casefold() or node[private_key] or node[fork_key]:
            continue
        if name.casefold() == f"{login}/{login}".casefold():
            continue
        selected.append((name, number(node.get("stargazers_count" if rest else "stargazerCount"), "star count")))
    return selected


def validate_calendar(raw_days: list[dict], start: date, end: date, reported_total: int | None = None) -> tuple[list[dict], int]:
    found: dict[date, int] = {}
    for day in raw_days:
        try:
            day_date = date.fromisoformat(day["date"])
        except (KeyError, TypeError, ValueError):
            raise StatsError("GitHub returned an invalid calendar date.") from None
        if start <= day_date <= end:
            if day_date in found:
                raise StatsError("GitHub returned duplicate calendar dates.")
            found[day_date] = number(day.get("count", day.get("contributionCount")), "daily contribution count")
    expected = days_between(start, end)
    if set(found) != set(expected):
        raise StatsError("The contribution calendar is incomplete; previous panels were kept.")
    total = sum(found.values())
    if reported_total is not None and number(reported_total, "contribution total") != total:
        raise StatsError("GitHub's contribution total does not match its calendar.")
    return [{"date": d.isoformat(), "count": found[d]} for d in expected], total


CALENDAR_QUERY = """
query ProfileCalendar($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    login
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

REPOSITORIES_QUERY = """
query PublicProjects($login: String!, $cursor: String) {
  user(login: $login) {
    login
    repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER],
                 privacy: PUBLIC, isFork: false, orderBy: {field: NAME, direction: ASC}) {
      nodes { nameWithOwner owner { login } isPrivate isFork stargazerCount }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def fetch_graphql_snapshot(login: str, now: datetime, token: str) -> dict:
    start, end = period(now)
    variables = {
        "login": login,
        "from": datetime.combine(start, time.min, timezone.utc).isoformat().replace("+00:00", "Z"),
        "to": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    user = graphql(CALENDAR_QUERY, variables, token)
    try:
        calendar = user["contributionsCollection"]["contributionCalendar"]
        raw_days = [d for week in calendar["weeks"] for d in week["contributionDays"]]
        days, total = validate_calendar(raw_days, start, end, calendar["totalContributions"])
    except (KeyError, TypeError):
        raise StatsError("GitHub returned incomplete contribution data.") from None
    selected: dict[str, int] = {}
    cursor, seen_cursors = None, set()
    while True:
        user = graphql(REPOSITORIES_QUERY, {"login": login, "cursor": cursor}, token)
        try:
            connection = user["repositories"]
            nodes, page = connection["nodes"], connection["pageInfo"]
            if not isinstance(nodes, list) or not isinstance(page.get("hasNextPage"), bool):
                raise StatsError("GitHub returned invalid repository pagination.")
            for name, stars in select_repositories(nodes, login):
                selected[name.casefold()] = stars
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise StatsError("GitHub returned a repeated repository cursor.")
            seen_cursors.add(cursor)
        except (KeyError, TypeError):
            raise StatsError("GitHub returned incomplete repository data.") from None
    return make_snapshot(login, start, end, days, total, len(selected), sum(selected.values()),
                         {"kind": "github_graphql", "endpoint": API + "/graphql", "checked_on": end.isoformat()})


class PublicCalendarParser(HTMLParser):
    """Read exact GitHub calendar tooltip counts; never infer counts from colors."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.dates: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.target: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in ("td", "rect") and attr.get("data-date") and attr.get("id"):
            self.dates[attr["id"]] = attr["data-date"]
            if re.fullmatch(r"\d+", str(attr.get("data-count", ""))):
                self.counts[attr["id"]] = int(attr["data-count"])
        if tag == "tool-tip" and attr.get("for"):
            self.target, self.text = attr["for"], []

    def handle_data(self, data):
        if self.target:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.target:
            match = re.match(r"\s*(No|\d[\d,]*) contributions? on\b", "".join(self.text))
            if match:
                self.counts[self.target] = 0 if match[1] == "No" else int(match[1].replace(",", ""))
            self.target, self.text = None, []

    def days(self) -> list[dict]:
        if not self.dates or any(key not in self.counts for key in self.dates):
            raise StatsError("Public calendar HTML did not expose complete exact counts.")
        return [{"date": value, "count": self.counts[key]} for key, value in self.dates.items()]


def fetch_public_snapshot(login: str, now: datetime) -> dict:
    """Initial review snapshot. A missing public calendar is explicitly unknown."""
    start, end = period(now)
    selected: dict[str, int] = {}
    repo_url = f"{API}/users/{quote(login, safe='')}/repos"
    page = 1
    while True:
        nodes = fetch_json(f"{repo_url}?per_page=100&type=owner&sort=full_name&page={page}")
        if not isinstance(nodes, list):
            raise StatsError("Public repository response was not a list.")
        for name, stars in select_repositories(nodes, login, rest=True):
            selected[name.casefold()] = stars
        if len(nodes) < 100:
            break
        page += 1
        if page > 1000:
            raise StatsError("Public repository pagination did not terminate.")
    urls, raw_days = [], []
    calendar_status = "available"
    try:
        for year in range(start.year, end.year + 1):
            url = f"https://github.com/users/{quote(login, safe='')}/contributions?from={year}-01-01&to={year}-12-31"
            urls.append(url)
            parser = PublicCalendarParser()
            parser.feed(fetch_bytes(url).decode("utf-8"))
            raw_days.extend(parser.days())
        days, total = validate_calendar(raw_days, start, end)
    except (StatsError, UnicodeDecodeError):
        days, total, calendar_status = [], None, "unavailable"
    snapshot = make_snapshot(login, start, end, days, total, len(selected), sum(selected.values()),
                             {"kind": "github_public_snapshot", "repositories_url": repo_url,
                              "calendar_urls": urls, "checked_on": end.isoformat()})
    snapshot["calendar_status"] = calendar_status
    return snapshot


def make_snapshot(login, start, end, days, total, projects, stars, source) -> dict:
    return {
        "schema_version": 1,
        "login": login,
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": 365, "timezone": "UTC"},
        "definitions": {
            "contributions": "Вклады, видимые в доступном календаре GitHub, за 365 дней; это не только коммиты. Публично доступные агрегаты закрытой активности могут входить в календарь.",
            "public_projects": "Собственные публичные репозитории без форков и без одноимённого репозитория профиля; архивные проекты включены.",
            "stars": "Полученные звёзды тех же публичных проектов.",
            "calendar": "Ровно 365 календарных дат UTC, включая текущую дату; текущий день ещё может измениться.",
        },
        "counts": {"contributions": total, "public_projects": projects, "stars": stars},
        "calendar_status": "available" if total is not None else "unavailable",
        "calendar": days,
        "source": source,
    }


def validate_snapshot(snapshot: dict) -> None:
    try:
        valid_login(snapshot["login"])
        start = date.fromisoformat(snapshot["period"]["start"])
        end = date.fromisoformat(snapshot["period"]["end"])
        if (end - start).days != 364 or snapshot["period"]["timezone"] != "UTC":
            raise StatsError("Snapshot does not cover 365 UTC dates.")
        counts = snapshot["counts"]
        number(counts["public_projects"], "project count")
        number(counts["stars"], "star count")
        if snapshot["calendar_status"] == "available":
            validate_calendar(snapshot["calendar"], start, end, counts["contributions"])
        elif snapshot["calendar_status"] == "unavailable":
            if counts["contributions"] is not None or snapshot["calendar"]:
                raise StatsError("Unavailable calendar must not contain inferred activity.")
        else:
            raise StatsError("Invalid calendar status.")
    except (KeyError, ValueError, TypeError):
        raise StatsError("Invalid statistics snapshot.") from None


def svg_text(text, x, y, size=18, color="#f0dfce", *, anchor="start", serif=False, weight="400") -> str:
    family = "Georgia, 'Times New Roman', serif" if serif else "'Segoe UI', Arial, sans-serif"
    return f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{escape(str(text))}</text>'


def svg_start(height: int, title: str, description: str) -> list[str]:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}" role="img" aria-labelledby="title desc">',
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
            '<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#120a0e"/><stop offset=".55" stop-color="#08090d"/><stop offset="1" stop-color="#210c13"/></linearGradient><linearGradient id="edge"><stop stop-color="#70202e"/><stop offset=".5" stop-color="#e2535a"/><stop offset="1" stop-color="#70202e"/></linearGradient></defs>',
            f'<rect x="1" y="1" width="998" height="{height-2}" rx="12" fill="url(#bg)" stroke="#5f202d"/>',
            f'<path d="M36 12H964L988 36V{height-36}L964 {height-12}H36L12 {height-36}V36Z" fill="none" stroke="#782837" stroke-width=".8"/>',
            f'<path d="M25 55V25H55 M945 25H975V55 M25 {height-55}V{height-25}H55 M945 {height-25}H975V{height-55}" fill="none" stroke="#d84d58" stroke-width="1.2"/>']


def date_label(snapshot: dict) -> str:
    return " — ".join(date.fromisoformat(snapshot["period"][k]).strftime("%d.%m.%Y") for k in ["start", "end"])


def render_stats(snapshot: dict) -> str:
    c = snapshot["counts"]
    description = (f"{snapshot['login']}. Вклады за 365 дней: {c['contributions'] if c['contributions'] is not None else 'данные не получены'}. "
                   f"Публичные проекты: {c['public_projects']}. Получено звёзд: {c['stars']}. "
                   f"Период {date_label(snapshot)}, UTC. " + snapshot["definitions"]["public_projects"])
    s = svg_start(308, "Летопись AsrtMe — статистика GitHub", description)
    s += [svg_text("Летопись", 46, 59, 37, serif=True),
          svg_text(date_label(snapshot) + " · UTC", 951, 56, 16, "#b89896", anchor="end"),
          '<path d="M46 82H954" stroke="url(#edge)" stroke-width=".9"/>',
          '<path d="M345 112V242 M655 112V242" stroke="#52212b"/>']
    labels = ["Вклады за 365 дней", "Публичные проекты", "Получено звёзд"]
    values = [c["contributions"], c["public_projects"], c["stars"]]
    for cx, label, value in zip([190, 500, 810], labels, values):
        s += [f'<path d="M{cx-6} 103L{cx} 97L{cx+6} 103L{cx} 109Z" fill="#cb3f50"/>',
              svg_text(label, cx, 142, 20, "#e3c9bf", anchor="middle", serif=True),
              svg_text("—" if value is None else f"{value:,}".replace(",", " "), cx, 215, 62, "#ffeadb", anchor="middle", serif=True)]
    s += [svg_text("Календарь GitHub" if c["contributions"] is not None else "Данные не получены", 190, 242, 14, "#b89194", anchor="middle"),
          svg_text("Без форков и профиля", 500, 242, 14, "#b89194", anchor="middle"),
          svg_text("У собственных проектов", 810, 242, 14, "#b89194", anchor="middle"),
          '<path d="M46 263H954" stroke="#54212b"/>',
          svg_text("[as]", 64, 287, 16, "#ed6a6b", serif=True),
          svg_text("Текущий день ещё может измениться", 936, 287, 13, "#a48589", anchor="end"),
          '</svg>']
    return "\n".join(s) + "\n"


def mobile_start(height: int, title: str, description: str) -> list[str]:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="420" height="{height}" viewBox="0 0 420 {height}" role="img" aria-labelledby="title desc">',
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
            '<defs><linearGradient id="mobilebg" x2="1" y2="1"><stop stop-color="#160b10"/><stop offset=".6" stop-color="#08090d"/><stop offset="1" stop-color="#250d15"/></linearGradient></defs>',
            f'<rect x="1" y="1" width="418" height="{height-2}" rx="12" fill="url(#mobilebg)" stroke="#782837"/>',
            f'<path d="M24 10H396L410 24V{height-24}L396 {height-10}H24L10 {height-24}V24Z" fill="none" stroke="#5f202d"/>']


def render_stats_mobile(snapshot: dict) -> str:
    c = snapshot["counts"]
    description = (f"{snapshot['login']}. Вклады за 365 дней: {c['contributions'] if c['contributions'] is not None else 'данные не получены'}. "
                   f"Публичные проекты: {c['public_projects']}. Получено звёзд: {c['stars']}. "
                   f"Период {date_label(snapshot)}, UTC. " + snapshot["definitions"]["public_projects"])
    s = mobile_start(532, "Летопись AsrtMe — статистика GitHub", description)
    s += [svg_text("Летопись",210,49,35,anchor="middle",serif=True),
          svg_text(date_label(snapshot) + " · UTC",210,76,13,"#b89896",anchor="middle")]
    labels = ["Вклады за 365 дней", "Публичные проекты", "Получено звёзд"]
    values = [c["contributions"], c["public_projects"], c["stars"]]
    descriptions = ["Календарь GitHub" if c["contributions"] is not None else "Данные не получены",
                    "Без форков и профиля", "У собственных проектов"]
    for index,(label,value,description) in enumerate(zip(labels,values,descriptions)):
        y = 97 + index*136
        s += [f'<path d="M30 {y}H390" stroke="#6e2735"/>',
              f'<path d="M204 {y}l6-5 6 5-6 5Z" fill="#cf4351"/>',
              svg_text(label,210,y+29,20,"#e3c9bf",anchor="middle",serif=True),
              svg_text("—" if value is None else f"{value:,}".replace(","," "),210,y+87,58,"#ffeadb",anchor="middle",serif=True),
              svg_text(description,210,y+114,14,"#b89194",anchor="middle")]
    s += [svg_text("[as] · текущий день ещё может измениться",210,515,12,"#a48589",anchor="middle"),'</svg>']
    return "\n".join(s) + "\n"


def calendar_layout(start: date, end: date) -> tuple[date, list[dict]]:
    """Monday-first columns; every UTC date has one cell, outside dates stay blank."""
    origin = start - timedelta(days=start.weekday())
    cells = []
    for day in days_between(start, end):
        offset = (day - origin).days
        cells.append({"date": day, "column": offset // 7, "row": day.weekday()})
    return origin, cells


def contribution_level(count: int) -> int:
    return 0 if count == 0 else 1 if count <= 2 else 2 if count <= 5 else 3 if count <= 9 else 4


def render_activity(snapshot: dict) -> str:
    start = date.fromisoformat(snapshot["period"]["start"])
    end = date.fromisoformat(snapshot["period"]["end"])
    description = ("Календарь вкладов GitHub за 365 дней. Столбцы — недели, строки — дни с понедельника по воскресенье. "
                   "Каждая руна — один день; более яркий красный цвет означает больше вкладов. "
                   f"Период {date_label(snapshot)}, UTC. ")
    if snapshot["calendar_status"] != "available":
        description += "Данные календаря пока не получены; ячейки не показаны."
    s = svg_start(298, "Гримуар — рунический календарь активности", description)
    s += [svg_text("Рунический календарь", 46, 52, 31, serif=True),
          svg_text("365 дней · UTC", 950, 51, 16, "#b89896", anchor="end"),
          '<path d="M46 71H954" stroke="url(#edge)"/>']
    if snapshot["calendar_status"] != "available":
        s += [svg_text("Печать ждёт данных", 500, 150, 28, "#e6c8bd", anchor="middle", serif=True),
              svg_text("Календарь появится после успешного получения статистики GitHub.", 500, 186, 17, "#b89896", anchor="middle")]
    else:
        lookup = {d["date"]: d["count"] for d in snapshot["calendar"]}
        _, cells = calendar_layout(start, end)
        x0, y0, step, size = 86, 112, 16, 12
        for row, name in [(0, "Пн"), (2, "Ср"), (4, "Пт"), (6, "Вс")]:
            s.append(svg_text(name, 65, y0 + row*step + 4, 12, "#ae8a90", anchor="end"))
        last_month = None
        last_label_x = -999
        for cell in cells:
            day = cell["date"]
            cx, cy = x0 + cell["column"] * step, y0 + cell["row"] * step
            if day.month != last_month:
                if cx - last_label_x >= 31:
                    s.append(svg_text(MONTHS[day.month-1], cx-5, 95, 12, "#bf9ba0"))
                    last_label_x = cx
                last_month = day.month
            count = lookup[day.isoformat()]
            level = contribution_level(count)
            title = f"{day.strftime('%d.%m.%Y')}: {count} вкладов"
            s.append(f'<path d="M{cx} {cy-size/2}L{cx+size/2} {cy}L{cx} {cy+size/2}L{cx-size/2} {cy}Z" fill="{PALETTE[level]}" stroke="{("#6c2432" if level else "#3a1b24")}" stroke-width=".6"><title>{escape(title)}</title></path>')
        legend = [("0",0),("1–2",1),("3–5",2),("6–9",3),("10+",4)]
        for i,(label,level) in enumerate(legend):
            lx = 552 + i*77
            s.append(f'<path d="M{lx} 236l6 6-6 6-6-6Z" fill="{PALETTE[level]}" stroke="#682332" stroke-width=".6"/>')
            s.append(svg_text(label,lx+12,247,12,"#c7a6a9"))
        s.append(svg_text("Каждая руна — один день", 46, 248, 14, "#bb989d"))
    s += [svg_text("Вклады, видимые в календаре GitHub · " + date_label(snapshot), 64, 280, 13, "#a8898f"), '</svg>']
    return "\n".join(s) + "\n"


def render_activity_mobile(snapshot: dict) -> str:
    end = date.fromisoformat(snapshot["period"]["end"])
    start = end - timedelta(days=end.weekday()+12*7)
    description = ("Последние 13 календарных недель, включая текущую неполную неделю. "
                   "Столбцы — недели, строки — дни с понедельника по воскресенье. "
                   f"Период {start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}, UTC. "
                   "Статистика вкладов в основном блоке охватывает все 365 дней.")
    s = mobile_start(337, "Гримуар — календарь последних 13 недель", description)
    s += [svg_text("Рунический календарь",210,42,26,anchor="middle",serif=True),
          svg_text("Последние 13 недель · UTC",210,69,15,"#b89896",anchor="middle"),
          '<path d="M25 86H395" stroke="#6e2735"/>']
    if snapshot["calendar_status"] != "available":
        s += [svg_text("Печать ждёт данных",210,166,24,"#e3c9bf",anchor="middle",serif=True),
              svg_text("Календарь пока не получен",210,198,16,"#b89896",anchor="middle")]
    else:
        lookup = {d["date"]: d["count"] for d in snapshot["calendar"]}
        _, cells = calendar_layout(start, end)
        x0,y0,step,size = 83,113,23,16
        for row,name in [(0,"Пн"),(2,"Ср"),(4,"Пт"),(6,"Вс")]:
            s.append(svg_text(name,61,y0+row*step+5,14,"#b89896",anchor="end"))
        for cell in cells:
            day = cell["date"]
            cx,cy = x0+cell["column"]*step,y0+cell["row"]*step
            count = lookup[day.isoformat()]
            level = contribution_level(count)
            title = f"{day.strftime('%d.%m.%Y')}: {count} вкладов"
            s.append(f'<path d="M{cx} {cy-size/2}L{cx+size/2} {cy}L{cx} {cy+size/2}L{cx-size/2} {cy}Z" fill="{PALETTE[level]}" stroke="{("#6c2432" if level else "#3a1b24")}" stroke-width=".7"><title>{escape(title)}</title></path>')
        for i,(label,level) in enumerate([("0",0),("1–2",1),("3–5",2),("6–9",3),("10+",4)]):
            lx = 48 + i*72
            s.append(f'<path d="M{lx} 277l6 6-6 6-6-6Z" fill="{PALETTE[level]}" stroke="#682332" stroke-width=".6"/>')
            s.append(svg_text(label,lx+11,288,13,"#c7a6a9"))
    dates = start.strftime('%d.%m.%Y') + " — " + end.strftime('%d.%m.%Y')
    s += [svg_text(dates,210,319,13,"#a8898f",anchor="middle"),'</svg>']
    return "\n".join(s) + "\n"


def write_outputs(snapshot: dict, output_dir: Path) -> list[str]:
    """Validate all results before replacing files; API calls have already finished."""
    validate_snapshot(snapshot)
    outputs = {
        "stats.svg": render_stats(snapshot),
        "stats-mobile.svg": render_stats_mobile(snapshot),
        "activity.svg": render_activity(snapshot),
        "activity-mobile.svg": render_activity_mobile(snapshot),
        "stats-data.json": json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }
    for filename in ("stats.svg", "stats-mobile.svg", "activity.svg", "activity-mobile.svg"):
        ET.fromstring(outputs[filename])
    output_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    changed = []
    try:
        for filename, content in outputs.items():
            destination = output_dir / filename
            encoded = content.encode("utf-8")
            if destination.is_file() and destination.read_bytes() == encoded:
                continue
            with tempfile.NamedTemporaryFile(dir=output_dir, prefix=".stats-", delete=False) as temp:
                temp.write(encoded)
                temporary = Path(temp.name)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            changed.append(destination.name)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="AsrtMe")
    parser.add_argument("--output", type=Path, default=ROOT / "assets")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--public-snapshot", action="store_true", help="Initial public preview; no credentials")
    mode.add_argument("--from-json", type=Path, help="Render an existing validated snapshot offline")
    args = parser.parse_args(argv)
    try:
        login = valid_login(args.user)
        now = datetime.now(timezone.utc)
        if args.from_json:
            snapshot = json.loads(args.from_json.read_text(encoding="utf-8"))
            if snapshot.get("login", "").casefold() != login.casefold():
                raise StatsError("Snapshot belongs to another GitHub user.")
        elif args.public_snapshot:
            snapshot = fetch_public_snapshot(login, now)
        else:
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise StatsError("GITHUB_TOKEN is required. Use --public-snapshot only for an initial public preview.")
            snapshot = fetch_graphql_snapshot(login, now, token)
        changed = write_outputs(snapshot, args.output)
        print(json.dumps({"status": "ok", "changed": changed, "counts": snapshot["counts"],
                          "calendar_status": snapshot["calendar_status"], "period": snapshot["period"]}, ensure_ascii=False))
        return 0
    except (StatsError, OSError, json.JSONDecodeError) as exc:
        print(f"Stats update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
