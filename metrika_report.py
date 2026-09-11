#!/usr/bin/env python3
"""
Ежедневный отчёт по Яндекс.Метрике для Cosmopolitan — Атлас памяти.

Что делает:
  1. Забирает статистику за вчера и за последние 7 дней из Яндекс.Метрики.
  2. Просит Claude написать короткий текстовый разбор: что изменилось,
     есть ли аномалии, на что обратить внимание.
  3. Собирает всё в простую HTML-страницу.
  4. Публикует её в GitHub-репозиторий (через Contents API) — если в
     репозитории включён GitHub Pages, страница обновляется сама.

Переменные окружения (задаются в Render → Environment):
  YANDEX_OAUTH_TOKEN   — токен Яндекс.Метрики (metrika:read)
  YANDEX_COUNTER_ID    — ID счётчика (по умолчанию 112345804)
  ANTHROPIC_API_KEY    — ключ Anthropic API (console.anthropic.com)
  GITHUB_TOKEN         — Personal Access Token с правом repo (contents: write)
  GITHUB_REPO          — "владелец/репозиторий", например "nokio7077-art/cosmopolitan-stats"
  GITHUB_BRANCH        — ветка публикации (по умолчанию "main")
  REPORT_PATH          — путь к файлу в репозитории (по умолчанию "index.html")
"""

import base64
import datetime
import json
import os
import sys

import requests

# ---------- конфигурация ----------

YANDEX_TOKEN = os.environ["YANDEX_OAUTH_TOKEN"]
COUNTER_ID = os.environ.get("YANDEX_COUNTER_ID", "112345804")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")  # опционально: без ключа отчёт считается по правилам
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
REPORT_PATH = os.environ.get("REPORT_PATH", "index.html")

METRIKA_URL = "https://api-metrika.yandex.net/stat/v1/data"
METRICS = [
    "ym:s:visits",
    "ym:s:users",
    "ym:s:pageviews",
    "ym:s:bounceRate",
    "ym:s:avgVisitDurationSeconds",
]
METRIC_LABELS = {
    "ym:s:visits": "Визиты",
    "ym:s:users": "Посетители",
    "ym:s:pageviews": "Просмотры",
    "ym:s:bounceRate": "Отказы, %",
    "ym:s:avgVisitDurationSeconds": "Средняя сессия, сек",
}


# ---------- Яндекс.Метрика ----------

def fetch_metrika(date1, date2, dimensions=None, metrics=None):
    params = {
        "ids": COUNTER_ID,
        "metrics": ",".join(metrics or METRICS),
        "date1": date1,
        "date2": date2,
        "accuracy": "full",
    }
    if dimensions:
        params["dimensions"] = dimensions
    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
    resp = requests.get(METRIKA_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def totals_dict(payload, metrics):
    """Достаёт агрегированные значения метрик из ответа Метрики."""
    totals = payload.get("totals") or []
    # totals может прийти как плоский список чисел
    flat = []
    for t in totals:
        if isinstance(t, list):
            flat.extend(t)
        else:
            flat.append(t)
    if not flat and payload.get("data"):
        flat = payload["data"][0].get("metrics", [])
    return {m: (flat[i] if i < len(flat) else None) for i, m in enumerate(metrics)}


def fetch_top_sources(date1, date2, limit=5):
    payload = fetch_metrika(date1, date2, dimensions="ym:s:trafficSource", metrics=["ym:s:visits"])
    rows = []
    for row in payload.get("data", []):
        dims = row.get("dimensions", [])
        name = dims[0]["name"] if dims else "—"
        metrics = row.get("metrics", [0])
        rows.append((name, metrics[0] if metrics else 0))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


# ---------- анализ без ИИ (по правилам) ----------

def pct_change(actual, average):
    if not average:
        return None
    return (actual - average) / average * 100.0


def rule_based_analysis(yesterday, week, sources):
    lines = []
    week_avg = {m: (week.get(m) or 0) / 7.0 for m in METRICS}

    visits_y = yesterday.get("ym:s:visits") or 0
    visits_avg = week_avg.get("ym:s:visits") or 0
    delta = pct_change(visits_y, visits_avg)

    if delta is None:
        lines.append(f"Вчера {fmt(visits_y)} визитов. Недостаточно истории для сравнения со средним.")
    else:
        direction = "выше" if delta >= 0 else "ниже"
        lines.append(f"Вчера {fmt(visits_y)} визитов — это {abs(delta):.0f}% {direction} среднего за 7 дней "
                      f"({fmt(round(visits_avg))}).")
        if delta >= 30:
            lines.append("Заметный всплеск трафика — стоит посмотреть, что его вызвало (источники ниже).")
        elif delta <= -30:
            lines.append("Заметное падение трафика по сравнению с обычным уровнем — возможно, стоит проверить сайт.")

    bounce_y = yesterday.get("ym:s:bounceRate")
    bounce_avg = week_avg.get("ym:s:bounceRate")
    if bounce_y is not None and bounce_avg:
        bdelta = bounce_y - bounce_avg
        if bdelta >= 10:
            lines.append(f"Отказы выше обычного ({bounce_y:.0f}% против ~{bounce_avg:.0f}% в среднем) — "
                          f"возможна проблема с загрузкой или нерелевантный источник трафика.")

    if sources:
        top_name, top_visits = sources[0]
        lines.append(f"Больше всего визитов за неделю дал источник «{top_name}» ({fmt(top_visits)}).")

    return " ".join(lines)


# ---------- анализ через Claude (опционально, если задан ANTHROPIC_API_KEY) ----------

def analyze_with_claude(yesterday, week, sources):
    prompt = (
        "Ты аналитик веб-трафика. Ниже данные Яндекс.Метрики для сайта-викторины "
        "по географии (Cosmopolitan — Атлас памяти) в JSON.\n\n"
        f"Вчера: {json.dumps(yesterday, ensure_ascii=False)}\n"
        f"Последние 7 дней (сумма): {json.dumps(week, ensure_ascii=False)}\n"
        f"Топ источников трафика за 7 дней (источник, визиты): {json.dumps(sources, ensure_ascii=False)}\n\n"
        "Напиши короткий отчёт на русском, до 150 слов: что изменилось по сравнению "
        "с обычным уровнем, есть ли что-то тревожное или, наоборот, обнадёживающее, "
        "и 1-2 конкретных совета. Пиши сразу текст отчёта, без вступлений и заголовков."
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text").strip()


# ---------- HTML-отчёт ----------

def fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_html(yesterday_vals, week_vals, sources, analysis, generated_at, report_date, analysis_source="правила"):
    cards = ""
    for m in METRICS:
        cards += f"""
        <div class="card">
          <div class="card-label">{METRIC_LABELS[m]}</div>
          <div class="card-value">{fmt(yesterday_vals.get(m))}</div>
          <div class="card-sub">за 7 дней: {fmt(week_vals.get(m))}</div>
        </div>"""

    sources_rows = "".join(
        f"<tr><td>{name}</td><td>{fmt(visits)}</td></tr>" for name, visits in sources
    ) or "<tr><td colspan='2'>Нет данных</td></tr>"

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Статистика Cosmopolitan — {report_date}</title>
<style>
  :root {{ --bg:#0d1520; --card:#152234; --accent:#c9a24b; --text:#e8ecf1; --muted:#8fa0b3; }}
  body {{ margin:0; padding:32px 16px; background:var(--bg); color:var(--text);
         font-family: -apple-system, Segoe UI, Roboto, sans-serif; }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:20px; font-weight:600; margin:0 0 4px; }}
  .updated {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:28px; }}
  .card {{ background:var(--card); border-radius:10px; padding:14px 16px; border:1px solid #24344a; }}
  .card-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .card-value {{ font-size:24px; font-weight:700; margin:4px 0 2px; color:var(--accent); }}
  .card-sub {{ color:var(--muted); font-size:12px; }}
  h2 {{ font-size:15px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; margin:28px 0 10px; }}
  .analysis {{ background:var(--card); border-left:3px solid var(--accent); border-radius:6px;
               padding:16px 18px; line-height:1.6; white-space:pre-line; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border-radius:8px; overflow:hidden; }}
  td {{ padding:10px 14px; border-bottom:1px solid #24344a; font-size:14px; }}
  td:last-child {{ text-align:right; color:var(--accent); font-weight:600; }}
  footer {{ margin-top:32px; color:var(--muted); font-size:12px; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>Cosmopolitan — Атлас памяти: статистика</h1>
    <div class="updated">Отчёт за {report_date} · обновлено {generated_at}</div>

    <h2>Вчера / 7 дней</h2>
    <div class="grid">{cards}</div>

    <h2>Разбор ({analysis_source})</h2>
    <div class="analysis">{analysis}</div>

    <h2>Источники трафика (7 дней)</h2>
    <table>{sources_rows}</table>

    <footer>Собрано автоматически из Яндекс.Метрики (счётчик {COUNTER_ID}) и проанализировано Claude.</footer>
  </div>
</body>
</html>"""


# ---------- публикация в GitHub Pages ----------

def publish_to_github(html):
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{REPORT_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    sha = None
    existing = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=30)
    if existing.status_code == 200:
        sha = existing.json().get("sha")

    payload = {
        "message": f"Обновление отчёта Метрики — {datetime.date.today()}",
        "content": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(api_url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------- main ----------

def main():
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    week_ago = today - datetime.timedelta(days=7)

    yesterday_payload = fetch_metrika(str(yesterday), str(yesterday))
    week_payload = fetch_metrika(str(week_ago), str(yesterday))
    yesterday_vals = totals_dict(yesterday_payload, METRICS)
    week_vals = totals_dict(week_payload, METRICS)
    sources = fetch_top_sources(str(week_ago), str(yesterday))

    analysis_source = "правила"
    if ANTHROPIC_KEY:
        try:
            analysis = analyze_with_claude(yesterday_vals, week_vals, sources)
            analysis_source = "ИИ (Claude)"
        except Exception as exc:  # не роняем весь отчёт, если ИИ-шаг не сработал
            print(f"Claude analysis failed, falling back to rule-based: {exc}", file=sys.stderr)
            analysis = rule_based_analysis(yesterday_vals, week_vals, sources)
    else:
        analysis = rule_based_analysis(yesterday_vals, week_vals, sources)

    generated_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = render_html(yesterday_vals, week_vals, sources, analysis, generated_at, str(yesterday), analysis_source)

    result = publish_to_github(html)
    print(f"Опубликовано: {result.get('content', {}).get('html_url', 'ok')}")


if __name__ == "__main__":
    main()
