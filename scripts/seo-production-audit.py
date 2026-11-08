#!/usr/bin/env python3
"""Проверка живого SEO-контура JobToo.

Скрипт не обращается к внутренней БД и не использует секреты: он смотрит на
сайт ровно так, как его видит поисковый робот. Результат годится для CI и для
ручного запуска после выкладки.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

UA = "JobToo-SEO-Audit/1.0 (+https://jobtoo.ru/)"


@dataclass
class Fetch:
    url: str
    status: int
    body: bytes
    content_type: str
    elapsed_ms: int
    error: str = ""
    attempts: int = 1


def fetch(url: str, timeout: float, attempts: int = 3) -> Fetch:
    """Fetch URL, retrying only transport failures.

    A transient timeout must not crash the whole audit before its JSON report
    is written. HTTP responses (including 4xx/5xx) are definitive and are not
    retried here; repeated transport failures are returned as status=0 so the
    caller can report them as a normal, actionable audit failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    started = time.monotonic()
    total_attempts = max(1, attempts)
    last_error = ""

    for attempt in range(1, total_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                status = int(getattr(r, "status", 200))
                ctype = r.headers.get("Content-Type", "")
            elapsed = round((time.monotonic() - started) * 1000)
            return Fetch(url, status, body, ctype, elapsed, attempts=attempt)
        except urllib.error.HTTPError as e:
            body = e.read()
            status = int(e.code)
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
            elapsed = round((time.monotonic() - started) * 1000)
            return Fetch(url, status, body, ctype, elapsed, attempts=attempt)
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < total_attempts:
                time.sleep(min(0.5 * attempt, 1.0))

    elapsed = round((time.monotonic() - started) * 1000)
    return Fetch(url, 0, b"", "", elapsed, error=last_error, attempts=total_attempts)


class PageFacts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonical = ""
        self.description = ""
        self.robots = ""
        self.title = ""
        self.h1 = 0
        self._in_title = False
        self._json_ld = False
        self.json_ld_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "link" and d.get("rel", "").lower() == "canonical":
            self.canonical = d.get("href", "")
        if tag.lower() == "meta":
            name = d.get("name", "").lower()
            if name == "description":
                self.description = d.get("content", "")
            if name == "robots":
                self.robots = d.get("content", "")
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() == "h1":
            self.h1 += 1
        if tag.lower() == "script" and d.get("type", "").lower() == "application/ld+json":
            self._json_ld = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() == "script" and self._json_ld:
            self._json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._json_ld:
            self.json_ld_chunks.append(data)


def page_facts(body: bytes) -> PageFacts:
    p = PageFacts()
    p.feed(body.decode("utf-8", errors="replace"))
    return p


def has_jobposting(p: PageFacts) -> bool:
    for chunk in p.json_ld_chunks:
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        objs = obj if isinstance(obj, list) else [obj]
        for item in objs:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return True
    return False


def percentile95(values: list[int]) -> int:
    if not values:
        return 0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(0.95 * (len(values) - 1))))
    return values[idx]


def sample_evenly(items: list[str], count: int) -> list[str]:
    if count <= 0 or len(items) <= count:
        return items[:]
    if count == 1:
        return [items[0]]
    out = []
    for i in range(count):
        idx = round(i * (len(items) - 1) / (count - 1))
        out.append(items[idx])
    return list(dict.fromkeys(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://jobtoo.ru")
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", dest="json_path", default="seo-production-report.json")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    failures: list[str] = []
    report: dict[str, object] = {"base": base, "checked_at_unix": int(time.time())}

    robots = fetch(base + "/robots.txt", args.timeout)
    robots_text = robots.body.decode("utf-8", errors="replace")
    report["robots"] = {
        "status": robots.status,
        "ms": robots.elapsed_ms,
        "attempts": robots.attempts,
        "error": robots.error,
    }
    if robots.error:
        failures.append(f"robots.txt transport после {robots.attempts} попыток: {robots.error}")
    elif robots.status != 200:
        failures.append(f"robots.txt HTTP {robots.status}")
    else:
        if f"Sitemap: {base}/sitemap.xml" not in robots_text:
            failures.append("robots.txt не указывает production sitemap")
        if re.search(r"(?mi)^User-agent:\s*\*\s*$[\s\S]{0,200}^Disallow:\s*/\s*$", robots_text):
            failures.append("robots.txt закрывает весь сайт")

    sitemap = fetch(base + "/sitemap.xml", args.timeout)
    report["sitemap_fetch"] = {
        "status": sitemap.status,
        "ms": sitemap.elapsed_ms,
        "attempts": sitemap.attempts,
        "error": sitemap.error,
        "content_type": sitemap.content_type,
        "bytes": len(sitemap.body),
    }
    urls: list[str] = []
    if sitemap.error:
        failures.append(f"sitemap transport после {sitemap.attempts} попыток: {sitemap.error}")
    elif sitemap.status != 200:
        failures.append(f"sitemap HTTP {sitemap.status}")
    else:
        try:
            root = ET.fromstring(sitemap.body)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = [n.text.strip() for n in root.findall("sm:url/sm:loc", ns) if n.text]
        except Exception as e:
            failures.append(f"sitemap XML не разбирается: {e}")

    parsed = [urlparse(u) for u in urls]
    foreign = [u for u, p in zip(urls, parsed) if p.netloc and p.netloc != urlparse(base).netloc]
    if foreign:
        failures.append(f"sitemap содержит чужие хосты: {foreign[:3]}")

    vacancy_urls = [u for u, p in zip(urls, parsed) if p.path.startswith("/v/") or p.path.startswith("/s/")]
    landing_urls = [u for u, p in zip(urls, parsed) if p.path.startswith("/rabota/")]
    static_urls = [u for u in urls if u not in vacancy_urls and u not in landing_urls]
    report["sitemap"] = {
        "total": len(urls),
        "vacancy_pages": len(vacancy_urls),
        "landing_pages": len(landing_urls),
        "static_pages": len(static_urls),
        "foreign_hosts": len(foreign),
    }
    if sitemap.status == 200 and len(urls) < 5:
        failures.append(f"sitemap подозрительно мал: {len(urls)} URL")

    checks: list[dict[str, object]] = []
    timings: list[int] = []

    for kind, pool, need_jobposting in (
        ("vacancy", vacancy_urls, True),
        ("landing", landing_urls, False),
    ):
        chosen = sample_evenly(pool, args.samples)
        for url in chosen:
            r = fetch(url, args.timeout)
            timings.append(r.elapsed_ms)
            item: dict[str, object] = {
                "kind": kind,
                "url": url,
                "status": r.status,
                "ms": r.elapsed_ms,
                "attempts": r.attempts,
                "error": r.error,
                "canonical": "",
                "title": "",
                "description_len": 0,
                "h1": 0,
                "robots": "",
                "jobposting": False,
            }
            checks.append(item)
            if r.error:
                failures.append(f"{kind} {url}: transport после {r.attempts} попыток: {r.error}")
                continue
            if r.status != 200:
                failures.append(f"{kind} {url}: HTTP {r.status}")
                continue

            facts = page_facts(r.body)
            item.update({
                "canonical": facts.canonical,
                "title": facts.title.strip(),
                "description_len": len(facts.description.strip()),
                "h1": facts.h1,
                "robots": facts.robots,
                "jobposting": has_jobposting(facts),
            })
            if facts.canonical.rstrip("/") != url.rstrip("/"):
                failures.append(f"{kind} {url}: canonical={facts.canonical!r}")
            if not facts.title.strip():
                failures.append(f"{kind} {url}: пустой title")
            if facts.h1 < 1:
                failures.append(f"{kind} {url}: нет h1")
            if "noindex" in facts.robots.lower():
                failures.append(f"{kind} {url}: неожиданно noindex")
            if need_jobposting:
                if not facts.description.strip():
                    failures.append(f"vacancy {url}: нет meta description")
                if not has_jobposting(facts):
                    failures.append(f"vacancy {url}: нет JobPosting JSON-LD")

    report["sample_checks"] = checks
    report["timings_ms"] = {
        "count": len(timings),
        "median": round(statistics.median(timings)) if timings else 0,
        "p95": percentile95(timings),
        "max": max(timings) if timings else 0,
    }
    report["partner_page_policy"] = {
        "individual_partner_pages_in_sitemap": False,
        "reason": "Не индексируем копии карточек источников; партнёрские данные участвуют в уникальных агрегированных /rabota/ страницах.",
    }
    report["failures"] = failures

    Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("JobToo production SEO audit")
    print(f"sitemap: total={len(urls)} vacancy={len(vacancy_urls)} landing={len(landing_urls)} static={len(static_urls)}")
    print(
        "sample timings: "
        f"median={report['timings_ms']['median']}ms "
        f"p95={report['timings_ms']['p95']}ms max={report['timings_ms']['max']}ms"
    )
    print(f"sampled pages: {len(checks)}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("result: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())