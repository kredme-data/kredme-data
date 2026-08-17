#!/usr/bin/env python3
"""
fetch.py — get one source URL, turn it into text, hash it.

Usage:
    python3 pipeline/fetch.py --url https://www.hdfc.bank.in/personal/pay/cards
    python3 pipeline/fetch.py --url https://.../mitc.pdf --no-pdfs --chars 2000

    from pipeline.fetch import fetch_source
    got = fetch_source(url)
    if got.ok and got.text:
        ...                      # got.text_sha256 is what state.has_changed reads

This module sits underneath everything else in the weekly pipeline: the
content-hash gate that decides which cards are worth paying a model to re-read,
the extractor, the verifier. Two properties matter more than anything else here.

1. IT NEVER RAISES. A weekly job that dies on one unreachable issuer stops the
   other 379 cards from being refreshed at all, and nobody notices a catalogue
   rotting quietly. Every failure comes back as data: ok=False and a readable
   `error`.

2. text_sha256 IS WHITESPACE-INSENSITIVE. Issuer pages reflow their markup
   constantly without changing a single number. If that read as "changed" we
   would pay to re-extract all 380 cards every week, which is the difference
   between a cheap week and an expensive one.

Stdlib only — urllib, gzip/zlib, html.parser, subprocess. The single external
dependency is the `pdftotext` binary (poppler), and its absence degrades to
"no text extracted", never an exception.

This module reports; it does not police. Whether a URL is an acceptable source
is config.is_issuer_domain()'s job, enforced at validate/publish time.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import gzip
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Running this file directly puts pipeline/ on sys.path rather than the repo
# root, and the CLI below is documented as a direct invocation.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config as C
from pipeline import state as S

# An issuer T&C PDF that takes longer than this to lay out is a scanned image,
# and pdftotext will return nothing useful from it however long we wait.
PDFTOTEXT_TIMEOUT_S = 120

# Tags whose text is never the card's terms. Their LINKS are still harvested
# (see _Extractor.handle_starttag) because the fees/MITC PDF is usually a
# footer link on exactly the pages whose footers we drop here.
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "footer", "svg", "template",
    "iframe", "canvas",
})

# Tags that end a line of prose. Without these, "Annual fee" and "Rs 500" from
# adjacent cells run together into one token and the model reads them as one
# phrase.
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "hr", "li", "ul", "ol", "tr", "td", "th", "table",
    "thead", "tbody", "section", "article", "header", "main", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dt", "dd", "dl",
    "label", "option", "title", "form", "figcaption",
})

# Characters that carry no meaning but do change a hash. Issuer CMSes sprinkle
# these through copy, and a soft hyphen appearing overnight must not read as a
# repriced card.
_ZERO_WIDTH = {
    0x200B: None,   # zero-width space
    0x200C: None,   # zero-width non-joiner
    0x200D: None,   # zero-width joiner
    0x2060: None,   # word joiner
    0xFEFF: None,   # BOM used mid-document
    0x00AD: None,   # soft hyphen
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class Fetched:
    """One fetch attempt, as data.

    `ok` means the transport worked and the server answered 2xx — nothing more.
    Text extraction is a separate axis: a PDF we could not read comes back
    ok=True, text="" and an `error` saying why. So when ok is True, `error` is
    advisory rather than fatal, and callers must test `text` before trusting it.

    `raw_sha256` hashes the bytes as they arrived on the wire (still compressed
    if the server compressed them), which makes it a debugging aid, not a change
    signal — a server flipping gzip on and off would move it. `text_sha256` is
    the one that drives change detection.
    """

    url: str
    ok: bool = False
    status: int = 0
    content_type: str = ""
    raw_sha256: str = ""
    text: str = ""
    text_sha256: str = ""
    error: str = ""
    linked_pdfs: list[str] = field(default_factory=list)
    # Every http(s) link on the page, for source discovery. Deliberately last and
    # defaulted so existing positional construction in tests keeps working.
    links: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Text normalisation — the money function
# ---------------------------------------------------------------------------
def normalise_text(s: str) -> str:
    """Collapse whitespace, drop zero-width characters, strip.

    This is what gets hashed. A page that only reflows its markup — an extra
    newline in a template, a tab becoming four spaces, a non-breaking space
    swapped for a plain one — must hash identically, or the incremental gate
    stops being incremental and every week costs a full 380-card re-extract.

    str.split() with no argument already treats NBSP and every other Unicode
    space as whitespace, so the join below handles them for free.
    """
    if not isinstance(s, str) or not s:
        return ""
    return " ".join(s.translate(_ZERO_WIDTH).split())


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _looks_like_pdf(raw: bytes, content_type: str) -> bool:
    """Magic bytes first: issuers serve PDFs as application/octet-stream, as
    text/html, and occasionally with no content-type at all."""
    if raw[:4] == b"%PDF" or b"%PDF-" in raw[:1024]:
        return True
    return "pdf" in (content_type or "").lower()


def _looks_like_html(raw: bytes, content_type: str) -> bool:
    if "html" in (content_type or "").lower():
        return True
    head = raw[:1024].lower()
    return b"<html" in head or b"<!doctype html" in head or b"<body" in head


def _charset_of(content_type: str) -> str:
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip("\"'")
    return ""


def _decode(raw: bytes, content_type: str) -> str:
    """Bytes to str, preferring the server's declared charset.

    errors='replace' at the end rather than a raise: a single bad byte in a
    footer must not cost us the fee table above it.
    """
    declared = _charset_of(content_type)
    if declared:
        try:
            return raw.decode(declared)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _pdf_to_text(raw: bytes) -> str:
    """Lay a PDF out as text using poppler's pdftotext, via stdin.

    Shelling out is deliberate. Summariser-style readers routinely report
    Flate-compressed issuer PDFs as empty or corrupt, and "empty" is
    indistinguishable from "this issuer publishes no reward rate" — a
    conclusion we would then record and act on. A tooling failure must never
    look like evidence of absence, so a missing binary returns nothing and the
    caller says so out loud.
    """
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=bytes(raw),
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError (no poppler installed) is an OSError. So is a
        # binary that exists but cannot be executed. Both mean "no text".
        return ""
    out = proc.stdout or b""
    if not isinstance(out, (bytes, bytearray)):
        return ""
    return bytes(out).decode("utf-8", errors="replace")


class _Extractor(HTMLParser):
    """Visible text and linked PDFs from one HTML page.

    The only non-dataclass class in this module: html.parser is extended by
    subclassing and there is no functional entry point. It replaces regex tag
    stripping, which reliably swallows an issuer's inline <script> block of JSON
    and hands it to the model as prose.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.chunks: list[str] = []
        self.pdfs: list[str] = []
        # Every http(s) href on the page, absolutised and defragged. `pdfs` is a
        # filtered view of this. Source discovery needs the rest: an issuer's card
        # listing page IS the index of its per-card documents, and without these
        # links every card of an issuer resolves to the same landing page.
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        # Links are harvested even inside a skipped region. The fees schedule
        # and MITC PDFs live in the footer on most issuer sites, so dropping
        # footer prose must not also drop the document that states the numbers.
        for name, value in attrs or ():
            if name and name.lower() == "href" and value:
                self._maybe_pdf(value)
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            # Real pages close tags they never opened; clamping at zero keeps
            # one stray </nav> from hiding the rest of the document.
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data:
            self.chunks.append(data)

    def _maybe_pdf(self, href: str) -> None:
        try:
            target = urllib.parse.urljoin(self.base_url, href.strip())
            # Drop the fragment: http.client would send "#page=3" as part of
            # the request path, and two anchors into one PDF are one document.
            target = urllib.parse.urldefrag(target)[0]
            split = urllib.parse.urlsplit(target)
        except ValueError:
            return
        if split.scheme.lower() not in ("http", "https"):
            return
        self.links.append(target)
        # Test the PATH, not the whole URL: issuer CDNs append cache-busting
        # query strings, and "…/mitc.pdf?v=7" is still a PDF.
        if not split.path.lower().endswith(".pdf"):
            return
        self.pdfs.append(target)


def _tidy_lines(text: str) -> str:
    """One line per block, no blank runs. Keeps structure the model can read
    while leaving the hash to normalise_text."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()


def _html_to_text(html: str, page_url: str) -> tuple[str, list[str]]:
    text, pdfs, _links = _html_to_text_and_links(html, page_url)
    return text, pdfs


def _html_to_text_and_links(html: str, page_url: str) -> tuple[str, list[str], list[str]]:
    """As _html_to_text, plus every http(s) link on the page.

    Kept separate so `extract_text` keeps its two-value signature — it has a lot
    of callers and tests — while discovery can get the links without parsing the
    document a second time.
    """
    parser = _Extractor(page_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Whatever parsed before the fault is still worth having, and a
        # malformed page is an issuer's problem we cannot fix by crashing.
        pass
    pdfs = list(dict.fromkeys(parser.pdfs))[: C.MAX_LINKED_PDFS]
    # Links are NOT capped by MAX_LINKED_PDFS: that budget bounds what we fetch,
    # and these are only read. An issuer listing page carries ~200 of them and
    # truncating to 4 would silently hide most of the catalogue from discovery.
    links = list(dict.fromkeys(parser.links))
    return _tidy_lines("".join(parser.chunks)), pdfs, links


def extract_text(raw: bytes, content_type: str, url: str) -> tuple[str, list[str]]:
    """Normalise fetched bytes to (text, linked_pdf_urls).

    Returns ("", []) rather than raising for anything it cannot read, including
    a missing pdftotext binary.
    """
    text, pdfs, _links = _extract_all(raw, content_type, url)
    return text, pdfs


def _extract_all(raw: bytes, content_type: str, url: str) -> tuple[str, list[str], list[str]]:
    """(text, linked_pdfs, all_links). The one place the three are derived."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return "", [], []
    raw = bytes(raw)
    ctype = content_type if isinstance(content_type, str) else ""
    page_url = url if isinstance(url, str) else ""

    if _looks_like_pdf(raw, ctype):
        return _pdf_to_text(raw), [], []
    if _looks_like_html(raw, ctype):
        return _html_to_text_and_links(_decode(raw, ctype), page_url)
    return _decode(raw, ctype), [], []


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _default_opener() -> Any:
    return urllib.request.build_opener().open


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": C.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )


def _status_of(resp: Any) -> int:
    for attr in ("status", "code"):
        value = getattr(resp, attr, None)
        if isinstance(value, int):
            return value
    getcode = getattr(resp, "getcode", None)
    if callable(getcode):
        try:
            value = getcode()
        except Exception:
            return 0
        if isinstance(value, int):
            return value
    return 0


def _header(resp: Any, name: str) -> str:
    """One response header, tolerant of plain dicts as well as email.Message."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return ""
    for key in (name, name.lower()):
        try:
            value = headers.get(key)
        except (AttributeError, TypeError):
            return ""
        if isinstance(value, str):
            return value
    return ""


def _close(resp: Any) -> None:
    closer = getattr(resp, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _read_capped(resp: Any) -> tuple[bytes, bool]:
    """Read at most MAX_FETCH_BYTES. Asks for one byte more so we can tell a
    file that exactly fits from one that was cut."""
    limit = C.MAX_FETCH_BYTES
    raw = resp.read(limit + 1)
    if raw is None:
        return b"", False
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    raw = bytes(raw)
    return raw[:limit], len(raw) > limit


def _inflate(raw: bytes, wbits: int) -> bytes:
    out = bytearray()
    inflater = zlib.decompressobj(wbits)
    try:
        out += inflater.decompress(raw)
        out += inflater.flush()
    except zlib.error:
        pass  # keep whatever decoded before the stream went bad
    return bytes(out)


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Undo Content-Encoding. A stream we cannot inflate is returned untouched.

    MAX_FETCH_BYTES can cut a gzip stream mid-block, so a failed whole-stream
    decompress falls back to salvaging the part that did arrive: half a fee
    table beats nothing, and the truncation is already reported separately.
    Servers also mislabel identity content as gzip, which is why an inflate
    that yields nothing returns the original bytes rather than an empty string.
    """
    enc = (encoding or "").lower()
    if "gzip" in enc or "x-gzip" in enc or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            return _inflate(raw, 16 + zlib.MAX_WBITS) or raw
    if "deflate" in enc:
        # "deflate" means zlib-wrapped in the RFC and raw-deflate in practice.
        return _inflate(raw, zlib.MAX_WBITS) or _inflate(raw, -zlib.MAX_WBITS) or raw
    return raw


def _retryable(status: int) -> bool:
    """A 5xx or a dead socket is worth asking again. A 404 is an answer, and a
    3xx that reached us unfollowed is a redirect loop — neither improves on a
    second attempt."""
    return status == 0 or status >= 500


def _backoff_s(attempt: int) -> float:
    return min(C.POLITE_DELAY_S * (2 ** attempt), 8.0)


def _finish(
    url: str,
    status: int,
    content_type: str,
    encoding: str,
    body: bytes,
    truncated: bool,
) -> Fetched:
    raw = _decompress(body, encoding)
    text, pdfs, links = _extract_all(raw, content_type, url)

    # A PDF fetched directly gets the PDF budget; anything else gets the page
    # budget. Both are what bounds the per-card token bill.
    is_pdf = _looks_like_pdf(raw, content_type)
    text = text[: C.MAX_PDF_CHARS if is_pdf else C.MAX_PAGE_CHARS]

    # pdftotext emits one form feed per page for a scanned or image-only PDF, so
    # the result is non-empty but carries no text and every caller's `if not text`
    # guard silently fails to fire. Collapse it here, once, rather than in each of
    # the three call sites. The hash is unaffected: normalise_text already maps a
    # whitespace-only document to "".
    if not text.strip():
        text = ""

    notes: list[str] = []
    if truncated:
        notes.append(f"body truncated at {C.MAX_FETCH_BYTES} bytes")
    if not text:
        notes.append(
            "no text extracted from PDF (is pdftotext/poppler installed?)"
            if is_pdf else "no text extracted"
        )

    # Hash the text we are actually keeping, after normalising it. Truncation
    # before hashing can only cause a needless re-extract of one card, never a
    # missed change, so it errs in the direction that costs money not accuracy.
    return Fetched(
        url=url,
        ok=True,
        status=status,
        content_type=content_type,
        raw_sha256=S.sha256_bytes(body),
        text=text,
        text_sha256=S.sha256_text(normalise_text(text)),
        error="; ".join(notes),
        linked_pdfs=pdfs,
        links=links,
    )


def fetch_url(url: str, *, opener: Any = None) -> Fetched:
    """GET `url`, decode it, hash it. Never raises.

    Retries config.FETCH_RETRIES times on a transport failure or a 5xx; a 4xx is
    returned immediately because it is an answer, not a hiccup.

    `opener` is the injection seam: any callable with urlopen's shape,
    opener(request, timeout=...) -> response. Tests pass a fake and never touch
    the network.
    """
    if not isinstance(url, str) or not url.strip():
        return Fetched(url=url if isinstance(url, str) else "", error="empty url")
    url = url.strip()
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError as exc:
        return Fetched(url=url, error=f"unparseable url: {exc}")
    if scheme not in ("http", "https"):
        return Fetched(url=url, error=f"unsupported scheme {scheme or '(none)'!r}")

    open_fn = opener or _default_opener()
    attempts = max(1, int(C.FETCH_RETRIES) + 1)
    status = 0
    error = "no attempt made"

    for attempt in range(attempts):
        content_type = ""
        encoding = ""
        body: bytes | None = None
        truncated = False
        try:
            resp = open_fn(_request(url), timeout=C.FETCH_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            # HTTPError is itself a readable response, but an error page's body
            # is not the card's terms, so we drop it.
            status = _status_of(exc)
            error = f"HTTP {status}"
            _close(exc)
            if not _retryable(status):
                return Fetched(url=url, status=status, error=error)
        except Exception as exc:
            # DNS, TLS, timeout, reset, and anything a fake opener throws at us.
            # Broad on purpose: one bad issuer must not end the weekly run.
            status = 0
            error = f"{type(exc).__name__}: {exc}"
        else:
            try:
                status = _status_of(resp)
                content_type = _header(resp, "Content-Type")
                encoding = _header(resp, "Content-Encoding")
                body, truncated = _read_capped(resp)
            except Exception as exc:
                status, body = 0, None
                error = f"read failed: {type(exc).__name__}: {exc}"
            finally:
                _close(resp)

            if body is not None:
                if 200 <= status < 300:
                    return _finish(url, status, content_type, encoding, body, truncated)
                error = f"HTTP {status}"
                if not _retryable(status):
                    return Fetched(url=url, status=status, error=error)

        if attempt + 1 < attempts:
            time.sleep(_backoff_s(attempt))

    return Fetched(url=url, status=status, error=error)


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def fetch_source(url: str, *, opener: Any = None, follow_pdfs: bool = True) -> Fetched:
    """Fetch a page and the PDFs it links to, as one document.

    Issuer sites publish the marketing copy in HTML and the numbers in a linked
    MITC/fees PDF, so a page fetched on its own frequently states no mechanic at
    all. The PDFs are appended under a banner carrying their URL, which keeps
    provenance visible to the extractor and to anyone reading a diff later.
    """
    page = fetch_url(url, opener=opener)
    if not page.ok or not follow_pdfs or not page.linked_pdfs:
        return page

    parts = [page.text]
    notes = [page.error] if page.error else []
    last_host = _host(page.url)

    for pdf_url in page.linked_pdfs[: C.MAX_LINKED_PDFS]:
        host = _host(pdf_url)
        if host and host == last_host:
            time.sleep(C.POLITE_DELAY_S)
        last_host = host
        got = fetch_url(pdf_url, opener=opener)
        if not got.ok or not got.text:
            notes.append(f"pdf skipped {pdf_url}: {got.error or 'no text'}")
            continue
        parts.append(f"\n\n=== PDF: {pdf_url} ===\n{got.text[: C.MAX_PDF_CHARS]}")

    text = "".join(parts)
    return dataclasses.replace(
        page,
        text=text,
        text_sha256=S.sha256_text(normalise_text(text)),
        error="; ".join(notes),
    )


def fetch_many(
    urls: "list[str] | tuple[str, ...]",
    *,
    opener: Any = None,
    follow_pdfs: bool = True,
    max_workers: int | None = None,
    on_progress: Any = None,
) -> dict[str, Fetched]:
    """Fetch a set of URLs, one host at a time but many hosts at once.

    Returns {url: Fetched}. Deduplicated: a URL appearing twice is fetched once.

    WHY THIS EXISTS
    ---------------
    The weekly refresh is designed to submit a batch and exit quickly, so that
    no job approaches Actions' 6-hour limit and the 2-hourly collector never
    finds it still running. That held when 373 cards resolved to 35 shared
    landing pages. Per-card source discovery took it to 196 distinct URLs, the
    sequential fetch grew past an hour, and `pipeline-advance` — same
    concurrency group — cancelled a refresh 60 minutes into its fetch.

    POLITENESS IS STRUCTURAL, NOT A SETTING
    ---------------------------------------
    Work is partitioned BY HOST and each host gets exactly one worker, which
    walks that host's URLs in order sleeping POLITE_DELAY_S between them. So no
    issuer ever sees two concurrent requests from us no matter how high
    max_workers goes, and there is no lock to get wrong. Raising the worker
    count adds hosts in flight, never requests per host.

    The busiest host is the critical path — today 38 distinct URLs — so more
    workers than there are hosts buys nothing.

    Like everything else in this module, it never raises: a URL that fails
    comes back as a Fetched with ok=False and a readable error.
    """
    # Dedupe while preserving first-seen order, so a shared landing page is
    # fetched once no matter how many cards point at it. 373 cards resolve to
    # 196 URLs today; this alone removes 177 redundant fetches.
    ordered = list(dict.fromkeys(u for u in urls if u))
    if not ordered:
        return {}

    by_host: dict[str, list[str]] = {}
    for url in ordered:
        by_host.setdefault(_host(url), []).append(url)

    results: dict[str, Fetched] = {}
    done = 0
    total = len(ordered)

    def _walk_one_host(host_urls: list[str]) -> list[tuple[str, Fetched]]:
        out: list[tuple[str, Fetched]] = []
        for i, url in enumerate(host_urls):
            if i:
                # Between requests to THIS host only. The first needs no wait.
                time.sleep(C.POLITE_DELAY_S)
            try:
                out.append((url, fetch_source(url, opener=opener, follow_pdfs=follow_pdfs)))
            except Exception as exc:  # pragma: no cover - fetch_source is no-raise
                # Belt and braces: an exception escaping into a worker would be
                # swallowed by the executor and the URL would vanish from the
                # results map, which reads downstream as "no cards changed".
                out.append((url, Fetched(url=url, error=f"{type(exc).__name__}: {exc}")))
        return out

    workers = max_workers if max_workers is not None else C.MAX_FETCH_WORKERS
    workers = max(1, min(workers, len(by_host)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_walk_one_host, group) for group in by_host.values()]
        for future in concurrent.futures.as_completed(futures):
            for url, got in future.result():
                results[url] = got
                done += 1
                if on_progress is not None:
                    on_progress(done, total)

    return results


# ---------------------------------------------------------------------------
# CLI — debugging one issuer by hand
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="fetch.py",
        description="Fetch one source URL and show exactly what the pipeline would see.",
    )
    ap.add_argument("--url", required=True, help="the page or PDF to fetch")
    ap.add_argument("--no-pdfs", action="store_true", help="do not follow linked PDFs")
    ap.add_argument("--chars", type=int, default=400, help="how much text to print (default 400)")
    args = ap.parse_args()

    got = fetch_source(args.url, follow_pdfs=not args.no_pdfs)
    preview = max(0, args.chars)

    print(f"url          {got.url}")
    print(f"ok           {got.ok}")
    print(f"status       {got.status}")
    print(f"content-type {got.content_type or '-'}")
    print(f"raw sha256   {got.raw_sha256 or '-'}")
    print(f"text sha256  {got.text_sha256 or '-'}")
    print(f"chars        {len(got.text)}")
    for pdf in got.linked_pdfs:
        print(f"linked pdf   {pdf}")
    if got.error:
        print(f"note         {got.error}")
    if not C.is_issuer_domain(got.url):
        print("note         not on the issuer allowlist — never source a rate from this")
    if got.text and preview:
        print(f"\n--- first {preview} chars ---")
        print(got.text[:preview])

    if not got.ok:
        return 1
    if not got.text:
        if "pdf" in (got.content_type or "").lower() and shutil.which("pdftotext") is None:
            print("\npdftotext (poppler) is not installed, so every PDF reads as empty.")
            print("Install it before trusting a 'no rate published' answer:  brew install poppler")
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
