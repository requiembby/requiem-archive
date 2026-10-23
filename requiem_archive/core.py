from __future__ import annotations

import hashlib
import ipaddress
import json
import posixpath
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


class Cancelled(Exception):
    pass


def normalize(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Enter an HTTP(S) URL without a username or password")
    host = p.hostname.encode("idna").decode().lower()
    if host == "localhost" or host.endswith((".local", ".localhost")):
        raise ValueError("Local addresses are not supported")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private IP addresses are not allowed")
    port = p.port
    if port and port not in (80, 443):
        raise ValueError("Only ports 80 and 443 are allowed")
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((p.scheme.lower(), authority, p.path or "/", p.query, ""))


def timestamp(value: str) -> str:
    if not re.fullmatch(r"\d{14}", value):
        raise ValueError("Snapshot timestamp must contain 14 digits")
    datetime.strptime(value, "%Y%m%d%H%M%S")
    return value


def local_path(url: str) -> str:
    p = urlsplit(url)
    suffix = Path(p.path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".html"
    return "files/" + hashlib.sha256(url.encode()).hexdigest() + suffix


@dataclass(frozen=True)
class Capture:
    timestamp: str
    original: str
    mimetype: str = ""


class ArchiveClient:
    def __init__(self, cancel=None, interval=1.5, session=None):
        self.cancel = cancel or threading.Event()
        self.interval = max(1.0, interval)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "requiem-archive/0.1 (+archive recovery; single worker)"})
        self.last = 0.0

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def get(self, url, params=None, limit=20 * 1024 * 1024):
        # Validate every hop; never follow an archive redirect to a live origin.
        for attempt in range(3):
            current = url
            for hop in range(6):
                self.check()
                p = urlsplit(current)
                if p.scheme != "https" or p.netloc != "web.archive.org":
                    raise ValueError("Blocked redirect outside web.archive.org")
                if self.cancel.wait(max(0, self.last + self.interval - time.monotonic())):
                    raise Cancelled()
                self.last = time.monotonic()
                with self.session.get(current, params=params if hop == 0 else None,
                                      timeout=(10, 25), allow_redirects=False, stream=True) as r:
                    if r.status_code in (429, 500, 502, 503, 504):
                        if attempt == 2:
                            r.raise_for_status()
                        delay = min(60, max(3 * (attempt + 1), int(r.headers.get("Retry-After", "0")) if r.headers.get("Retry-After", "").isdigit() else 0))
                        if self.cancel.wait(delay):
                            raise Cancelled()
                        break
                    if r.is_redirect:
                        current = urljoin(r.url, r.headers["Location"])
                        continue
                    r.raise_for_status()
                    data = bytearray()
                    for chunk in r.iter_content(65536):
                        self.check()
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError("Response size limit exceeded")
                    return bytes(data), r.headers.get("Content-Type", ""), current
            else:
                raise ValueError("Too many redirects")
        raise RuntimeError("The archive is temporarily unavailable")

    def timeline(self, url, limit=500):
        url = normalize(url)
        data, _, _ = self.get("https://web.archive.org/cdx/search/cdx", {
            "url": url, "output": "json", "fl": "timestamp,original,mimetype",
            "filter": "statuscode:200", "collapse": "timestamp:6", "limit": limit,
        }, limit=2 * 1024 * 1024)
        rows = json.loads(data)
        if not rows:
            return []
        if rows[0] != ["timestamp", "original", "mimetype"]:
            raise ValueError("Unexpected CDX response")
        return [Capture(timestamp(row[0]), normalize(row[1]), row[2]) for row in rows[1:]]

    def fetch(self, url, stamp):
        return self.get(f"https://web.archive.org/web/{timestamp(stamp)}id_/{quote(normalize(url), safe=':/?=&%+;,@!$()*[]~-')}")


CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)|@import\s+(['\"])(.*?)\3", re.I)


def rewrite(data, mime, url, resolve):
    def css(text):
        def sub(m):
            raw = m.group(2) if m.group(2) is not None else m.group(4)
            target = resolve(raw, url)
            return f'url("{target}")' if m.group(2) is not None else f'@import "{target}"'
        return CSS_URL.sub(sub, text)

    if "html" in mime:
        soup = BeautifulSoup(data, "html.parser")
        base_tag = soup.find("base", href=True)
        base = urljoin(url, base_tag["href"]) if base_tag else url
        for tag in soup.select("script, iframe, object, embed, base, meta[http-equiv], link[rel='preload']"):
            tag.decompose()
        for tag in soup.find_all(True):
            for attr in list(tag.attrs):
                if attr.lower().startswith("on") or attr in ("integrity", "srcdoc", "nonce"):
                    del tag[attr]
            for attr in ("href", "src", "poster", "background"):
                if tag.get(attr):
                    tag[attr] = resolve(tag[attr], base)
            if tag.get("srcset"):
                # Avoid ambiguous comma parsing, especially data URLs.
                del tag["srcset"]
            if tag.get("style"):
                tag["style"] = css(tag["style"])
            if tag.name == "style" and tag.string:
                tag.string = css(tag.string)
            if tag.name == "form":
                tag["action"] = "#"
        head = soup.head
        if head is None:
            head = soup.new_tag("head")
            soup.insert(0, head)
        meta = soup.new_tag("meta", charset="utf-8")
        head.insert(0, meta)
        csp = soup.new_tag("meta")
        csp["http-equiv"] = "Content-Security-Policy"
        csp["content"] = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; media-src 'self'; form-action 'none'; base-uri 'none'"
        head.insert(1, csp)
        return str(soup).encode("utf-8")
    if "css" in mime:
        return css(data.decode("utf-8", errors="replace")).encode("utf-8")
    return data


class Recovery:
    def __init__(self, client, progress=lambda message: None):
        self.client, self.progress = client, progress

    def run(self, url, stamp, output, max_files=100):
        url, stamp = normalize(url), timestamp(stamp)
        if not 1 <= max_files <= 10000:
            raise ValueError("File limit: 1–10000")
        root = Path(output).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ValueError("Choose an empty folder: existing files will not be overwritten")
        (root / "files").mkdir()
        queue, seen, saved = deque([url]), {url}, {}
        report = {"url": url, "requested_timestamp": stamp, "status": "running", "files": [], "limited": False}

        def resolve(raw, base):
            if raw.startswith("#"):
                return raw
            try:
                absolute, fragment = urldefrag(urljoin(base, raw))
                # Unwrap replay URLs without ever fetching the original host.
                match = re.match(r"https?://web\.archive\.org/web/\d+(?:[a-z]+_)?/(https?://.*)", absolute)
                absolute = normalize(match.group(1) if match else absolute)
                if urlsplit(absolute).hostname != urlsplit(url).hostname:
                    return "#unavailable-external"
                if absolute not in seen:
                    if len(seen) >= max_files:
                        report["limited"] = True
                        return "#recovery-limit"
                    seen.add(absolute)
                    queue.append(absolute)
                relative = posixpath.relpath(local_path(absolute), "files")
                return relative + ("#" + fragment if fragment else "")
            except (ValueError, UnicodeError):
                return "#unavailable"

        try:
            while queue:
                self.client.check()
                current = queue.popleft()
                entry = {"url": current, "path": local_path(current)}
                self.progress(f"{len(report['files']) + 1}/{max_files}  {current}")
                try:
                    data, mime, replay = self.client.fetch(current, stamp)
                    entry.update(status="saved", mime=mime, replay_url=replay)
                    data = rewrite(data, mime, current, resolve)
                    (root / entry["path"]).write_bytes(data)
                    saved[current] = entry["path"]
                except Cancelled:
                    raise
                except (requests.RequestException, ValueError, RuntimeError) as exc:
                    entry.update(status="failed", error=str(exc))
                report["files"].append(entry)
            report["status"] = "partial" if report["limited"] or any(e["status"] == "failed" for e in report["files"]) else "complete"
        except Cancelled:
            report["status"] = "cancelled"
        finally:
            # Rewrite only targets actually saved; missing targets become local placeholders.
            for missing in seen - saved.keys():
                (root / local_path(missing)).write_text("<!doctype html><meta charset='utf-8'><title>Unavailable</title><h1>File not recovered</h1><p>See report.json</p>", encoding="utf-8")
            if url in saved:
                (root / "index.html").write_text(f'<!doctype html><meta charset="utf-8"><a href="{saved[url]}">Open recovered website</a>', encoding="utf-8")
            (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
