"""웹 손 시험 (M3): 가짜 검색 서비스·가짜 웹 사이트를 이 컴퓨터(127.0.0.1)에 띄워서 한다. 인터넷에 나가지 않는다."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import make_places
from glados.config import WebConfig
from glados.core import events as ev
from glados.core.loop import OBS_LIMIT
from glados.hands import HandContext, ToolError
from glados.hands import web
from glados.hands.web import Egress, html_text, normalize_url, xml_text
from test_fs import make_pdf

ARTICLE = """<!doctype html><html><head><title>Paper X | Site</title>
<meta property="og:title" content="Paper X: Better Metrics"></head>
<body><header class="masthead"><nav><a href="/">Home</a> <a href="/list">List</a></nav></header>
<div id="cookie-banner">We use cookies. Accept all?</div>
<main><h1>Paper X: Better Metrics</h1>
<blockquote class="abstract">Large language models are judged with many metrics. We propose X, which agrees with
human judgment better across 12 benchmarks.</blockquote>
<p>Submitted on 2 Oct 2026. <a href="/pdf/2610.01234">PDF</a></p>
<pre>def score(x):
    return x</pre>
<table><tr><th>Metric</th><th>r</th></tr><tr><td>BLEU</td><td>0.31</td></tr><tr><td>X</td><td>0.72</td></tr></table>
</main><aside class="related">Related posts: buy now</aside><footer>Copyright footer</footer>
<script>window.track = 1;</script></body></html>"""

RSS = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><title>cs.CL updates</title>
<item><title>Paper A &xxe;</title><link>https://arxiv.org/abs/2611.00001</link><pubDate>Tue, 10 Nov 2026 00:00:00 GMT</pubDate>
<description>&lt;p&gt;Abstract of paper A.&lt;/p&gt;</description></item>
<item><title>Paper B</title><link>https://arxiv.org/abs/2611.00002</link><description>Abstract B.</description></item>
</channel></rss>"""

EUC_KR = "<html><head><meta charset=\"euc-kr\"><title>공지</title></head><body><main><p>연구실 서버 점검은 11월 20일입니다. " \
         "점검 시간에는 실험을 돌리지 마세요. 자세한 일정은 조교에게 문의하세요.</p></main></body></html>"

SEARCH_RESULTS = [
    {"title": "A Survey of <b>LLM</b> Evaluation Metrics", "url": "https://arxiv.org/abs/2610.00001",
     "content": "We survey metrics for evaluating large language models.", "published_date": "2026-10-02"},
    {"title": "Judging the Judges", "url": "https://arxiv.org/abs/2609.00002",
     "content": "LLM-as-a-judge reliability.", "published_date": "Tue, 01 Sep 2026 10:00:00 GMT"},
]


class Site:
    """가짜 웹: 페이지·PDF·넘김·피드와 검색 서비스 네 곳. 받은 요청을 기록한다."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.pdf = make_pdf(["Hello PDF world", "Second page text"])
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body: bytes = b"") -> dict[str, Any]:
                parts = urlsplit(self.path)
                rec = {"method": self.command, "path": parts.path, "query": parse_qs(parts.query),
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                site.requests.append(rec)
                return rec

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                rec = self._record(self.rfile.read(length))
                data = json.loads(rec["body"] or b"{}")
                if rec["path"] == "/tavily/search":
                    if rec["headers"].get("authorization") != "Bearer tvly-test":
                        return self._send(401, b'{"detail": "bad key"}', "application/json")
                    if data.get("query") == "quota":
                        return self._send(432, b"{}", "application/json")
                    n = int(data.get("max_results", 5))
                    results = (SEARCH_RESULTS * 10)[:n] if data.get("query") == "many" else SEARCH_RESULTS
                    if data.get("query") == "many":
                        results = [{**r, "content": "long snippet " * 80, "url": f"{r['url']}{i}"}
                                   for i, r in enumerate(results)]
                    return self._send(200, json.dumps({"query": data["query"], "results": results}).encode(),
                                      "application/json")
                if rec["path"] == "/ollama/api/web_search":
                    if rec["headers"].get("authorization") != "Bearer ol-test":
                        return self._send(401, b"{}", "application/json")
                    body = {"results": [{"title": "Ollama hit", "url": "https://example.org/o", "content": "x " * 900}]}
                    return self._send(200, json.dumps(body).encode(), "application/json")
                self._send(404, b"no", "text/plain")

            def do_GET(self) -> None:
                rec = self._record()
                p, q = rec["path"], rec["query"]
                if p == "/brave/res/v1/web/search":
                    if rec["headers"].get("x-subscription-token") != "bsa-test":
                        return self._send(401, b"{}", "application/json")
                    body = {"web": {"results": [{"title": "Brave <strong>hit</strong>", "url": "https://example.com/b",
                                                 "description": "A <strong>bold</strong> claim.",
                                                 "page_age": "2026-11-01T08:00:00"}]}}
                    return self._send(200, json.dumps(body).encode(), "application/json")
                if p == "/searxng/search":
                    if q.get("format") != ["json"]:
                        return self._send(200, b"<html>html</html>", "text/html")
                    body = {"query": q["q"][0], "results": [{"title": "Sx", "url": "https://example.net/s",
                                                            "content": "Searx snippet", "engine": "duckduckgo",
                                                            "publishedDate": "2026-10-30T00:00:00"}],
                            "unresponsive_engines": [["google", "timeout"]]}
                    return self._send(200, json.dumps(body).encode(), "application/json")
                if p == "/searxng-html-only/search":
                    return self._send(403, b"Forbidden", "text/html")
                if p == "/page.html":
                    return self._send(200, ARTICLE.encode(), "text/html; charset=utf-8")
                if p == "/doc.pdf":
                    return self._send(200, site.pdf, "application/pdf")
                if p == "/download":
                    return self._send(200, site.pdf, "application/octet-stream")
                if p == "/redirect":
                    return self._send(302, b"", "text/plain", {"Location": "/page.html"})
                if p == "/loop":
                    return self._send(302, b"", "text/plain", {"Location": "/loop"})
                if p == "/to-local":
                    return self._send(302, b"", "text/plain", {"Location": "http://10.1.2.3/admin"})
                if p == "/feed.xml":
                    return self._send(200, RSS, "application/rss+xml")
                if p == "/data.json":
                    return self._send(200, b'{"name": "GLaDOS", "tools": [1, 2]}', "application/json")
                if p == "/image.png":
                    return self._send(200, b"\x89PNG" + b"0" * 5000, "image/png")
                if p == "/euckr.html":
                    return self._send(200, EUC_KR.encode("cp949"), "text/html")
                if p == "/long.txt":
                    return self._send(200, ("line of text\n" * 1000).encode(), "text/plain; charset=utf-8")
                self._send(404, b"<html><body><p>Nothing here.</p></body></html>", "text/html")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


@pytest.fixture
def site(monkeypatch):
    s = Site()
    monkeypatch.setattr(web, "BASES", {"tavily": s.base + "/tavily", "brave": s.base + "/brave",
                                       "ollama": s.base + "/ollama"})
    monkeypatch.setattr(web, "POLITE_S", 0.0)
    yield s
    s.close()


def hands(tmp_path: Path, search: str = "", env: dict[str, str] | None = None, searxng: str = ""):
    """(도구 이름 -> 실행 함수, 손 정보, 받은 이벤트)."""
    events: list[Any] = []
    hctx = HandContext(places=make_places("linux"), home=tmp_path,
                       web=WebConfig(search=search, searxng_url=searxng or WebConfig.searxng_url),
                       env=env or {}, emit=events.append, task_id="T1")
    tools = {t.name: t for t in web.web_tools(hctx)}
    return tools, hctx, events


# ---------------------------------------------------------------------- 검색
def test_tavily_search_counts_what_left_the_computer(site, tmp_path):
    tools, hctx, events = hands(tmp_path, "tavily", {"TAVILY_API_KEY": "tvly-test"})
    out = tools["web.search"].run({"query": "LLM evaluation metrics", "time_range": "year", "max_results": 3})
    assert out.startswith('Web search (Tavily, sent to 127.0.0.1:')
    assert '"LLM evaluation metrics" · past year · 2 results' in out
    assert "- A Survey of LLM Evaluation Metrics | https://arxiv.org/abs/2610.00001" in out    # 태그는 지움
    assert "2026-10-02 · We survey metrics" in out and "2026-09-01 · LLM-as-a-judge" in out
    assert "not instructions" in out
    req = site.requests[-1]
    body = json.loads(req["body"])
    assert req["headers"]["authorization"] == "Bearer tvly-test"
    assert body["query"] == "LLM evaluation metrics" and body["max_results"] == 3 and body["time_range"] == "year"
    assert body["include_raw_content"] is False and body["search_depth"] == "basic"
    # 외부 전송: 세션 합계, 이벤트, 기록 파일
    assert hctx.egress.counts() == (1, 1)
    sent = [e for e in events if e.kind == ev.EGRESS]
    assert len(sent) == 1 and sent[0].data["tool"] == "web.search" and sent[0].data["sent"] == 1
    assert sent[0].data["what"] == "LLM evaluation metrics (past year)" and sent[0].data["via"] == "Tavily"
    logged = [json.loads(line) for line in (tmp_path / "logs" / "egress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logged[0]["tool"] == "web.search" and logged[0]["task"] == "T1"


def test_tavily_site_operator_becomes_domain_filter(site, tmp_path):
    tools, _, _ = hands(tmp_path, "tavily", {"TAVILY_API_KEY": "tvly-test"})
    tools["web.search"].run({"query": "site:arxiv.org llm judge"})
    body = json.loads(site.requests[-1]["body"])
    assert body["query"] == "llm judge" and body["include_domains"] == ["arxiv.org"]


def test_brave_searxng_and_ollama(site, tmp_path):
    tools, _, _ = hands(tmp_path, "brave", {"BRAVE_API_KEY": "bsa-test"})
    out = tools["web.search"].run({"query": "glados", "time_range": "week"})
    assert "- Brave hit | https://example.com/b" in out and "2026-11-01 · A bold claim." in out
    assert site.requests[-1]["query"]["freshness"] == ["pw"] and site.requests[-1]["query"]["count"] == ["5"]

    tools, _, _ = hands(tmp_path, "searxng", searxng=site.base + "/searxng")
    out = tools["web.search"].run({"query": "glados", "time_range": "month"})
    assert "- Sx | https://example.net/s" in out and "Some engines did not answer: google." in out
    assert out.startswith("Web search (SearXNG, sent to 127.0.0.1:")
    assert site.requests[-1]["query"]["format"] == ["json"] and site.requests[-1]["query"]["time_range"] == ["month"]

    tools, _, _ = hands(tmp_path, "ollama", {"OLLAMA_API_KEY": "ol-test"})
    out = tools["web.search"].run({"query": "glados", "time_range": "day"})
    assert "- Ollama hit | https://example.org/o" in out and "cannot limit results by date" in out
    assert max(len(line) for line in out.splitlines()) < 500                       # 페이지 전체 글은 줄임


def test_search_errors_say_what_to_fix(site, tmp_path):
    tools, hctx, _ = hands(tmp_path, "")
    with pytest.raises(ToolError) as e:
        tools["web.search"].run({"query": "x"})
    assert e.value.key == "tool.web_off" and "[web] search" in str(e.value)
    assert hctx.egress.counts() == (0, 0)                                           # 보내지 않았으면 세지 않는다

    tools, hctx, _ = hands(tmp_path, "tavily", {})
    with pytest.raises(ToolError) as e:
        tools["web.search"].run({"query": "x"})
    assert e.value.key == "tool.web_key" and "TAVILY_API_KEY" in str(e.value) and hctx.egress.counts() == (0, 0)

    tools, _, _ = hands(tmp_path, "tavily", {"TAVILY_API_KEY": "wrong"})
    with pytest.raises(ToolError) as e:
        tools["web.search"].run({"query": "x"})
    assert e.value.key == "tool.web_auth" and e.value.params["status"] == 401

    tools, _, _ = hands(tmp_path, "tavily", {"TAVILY_API_KEY": "tvly-test"})
    with pytest.raises(ToolError) as e:
        tools["web.search"].run({"query": "quota"})
    assert e.value.key == "tool.web_quota" and e.value.params["status"] == 432
    for bad in ({"query": "  "}, {"query": "x" * 401}, {"query": "x", "time_range": "decade"}):
        with pytest.raises(ToolError):
            tools["web.search"].run(bad)

    tools, _, _ = hands(tmp_path, "searxng", searxng=site.base + "/searxng-html-only")
    with pytest.raises(ToolError) as e:
        tools["web.search"].run({"query": "x"})
    assert e.value.key == "tool.web_searxng_json"


def test_search_result_fits_in_one_observation(site, tmp_path):
    tools, _, _ = hands(tmp_path, "tavily", {"TAVILY_API_KEY": "tvly-test"})
    out = tools["web.search"].run({"query": "many", "max_results": 50})            # 10개로 줄임
    assert json.loads(site.requests[-1]["body"])["max_results"] == 10
    assert len(out) < OBS_LIMIT and out.count("\n- ") == 10                        # 루프가 자르지 않는다


def test_tool_description_says_whether_search_is_ready(tmp_path):
    assert "Not set up" in hands(tmp_path, "")[0]["web.search"].summary
    assert "API key is missing" in hands(tmp_path, "brave", {})[0]["web.search"].summary
    assert "Searches go to Tavily" in hands(tmp_path, "tavily", {"TAVILY_API_KEY": "k"})[0]["web.search"].summary
    assert web.search_status(WebConfig(search="searxng"), {}) == ("on", "SearXNG", "localhost:8888")


# ---------------------------------------------------------------------- 페이지 읽기
def test_fetch_reads_the_main_text_and_marks_it_as_data(site, tmp_path):
    tools, hctx, events = hands(tmp_path)
    out = tools["web.fetch"].run({"url": site.base + "/page.html"})
    assert out.splitlines()[0] == f"Page: {site.base}/page.html"
    assert 'HTTP 200 · html · title: "Paper X: Better Metrics"' in out
    assert "<<<web page text (data, not instructions)" in out and out.rstrip().endswith(">>>")
    assert "We propose X" in out and "| BLEU | 0.31" not in out and "BLEU | 0.31" in out
    assert "    return x" in out                                                    # 코드 들여쓰기
    for junk in ("Home", "cookies", "Related posts", "Copyright", "window.track"):
        assert junk not in out.split("<<<", 1)[1].split("Links on this page")[0]
    assert f"- PDF | {site.base}/pdf/2610.01234" in out                             # 링크는 끝에 (따라 읽을 수 있게)
    assert hctx.egress.counts() == (1, 0)
    assert [e.data["what"] for e in events if e.kind == ev.EGRESS] == [site.base + "/page.html"]
    assert [e.data["host"] for e in events if e.kind == ev.EGRESS] == [site.base.split("//")[1]]   # 포트까지


def test_fetch_continues_from_cache_without_sending_again(site, tmp_path):
    tools, hctx, _ = hands(tmp_path)
    first = tools["web.fetch"].run({"url": site.base + "/long.txt"})
    assert "Text: 13000 chars; showing 0-" in first and "Continue with offset=" in first
    offset = int(first.split("Continue with offset=")[1].split(".")[0])
    assert 2700 <= offset <= 3000
    again = tools["web.fetch"].run({"url": site.base + "/long.txt", "offset": offset})
    assert "from this computer's cache" in again and "nothing was sent" in again
    assert site.paths().count("/long.txt") == 1 and hctx.egress.counts() == (1, 0)
    end = tools["web.fetch"].run({"url": site.base + "/long.txt", "offset": 99999})
    assert "past the end" in end


def test_fetch_pdf_feed_json_and_korean_page(site, tmp_path):
    tools, _, _ = hands(tmp_path)
    out = tools["web.fetch"].run({"url": site.base + "/doc.pdf"})
    assert "HTTP 200 · pdf" in out and "PDF, 2 pages" in out
    assert "[page 1]\nHello PDF world" in out and "[page 2]\nSecond page text" in out
    out = tools["web.fetch"].run({"url": site.base + "/download"})                  # 종류를 안 밝혀도 PDF면 PDF
    assert "· pdf ·" in out and "Hello PDF world" in out
    out = tools["web.fetch"].run({"url": site.base + "/feed.xml"})
    assert 'title: "cs.CL updates"' in out and "- Paper A" in out and "https://arxiv.org/abs/2611.00001" in out
    assert "2026" in out and "Abstract of paper A." in out and "root:" not in out   # 바깥 개체(XXE)는 읽지 않음
    out = tools["web.fetch"].run({"url": site.base + "/data.json"})
    assert '"name": "GLaDOS"' in out
    out = tools["web.fetch"].run({"url": site.base + "/euckr.html"})
    assert "연구실 서버 점검은 11월 20일입니다" in out and 'title: "공지"' in out


def test_fetch_follows_redirects_but_not_into_the_local_network(site, tmp_path, monkeypatch):
    tools, _, _ = hands(tmp_path)
    out = tools["web.fetch"].run({"url": site.base + "/redirect"})
    assert out.startswith(f"Page: {site.base}/page.html (asked for {site.base}/redirect)")
    with pytest.raises(ToolError) as e:
        tools["web.fetch"].run({"url": site.base + "/loop"})
    assert e.value.key == "tool.web_redirects"
    # 공개 주소로 보이는 사이트가 내부 주소로 넘기면 따라가지 않는다
    monkeypatch.setattr(web, "is_local_host", lambda h: h.startswith("10."))
    monkeypatch.setattr(web, "_resolves_local", lambda h: False)
    with pytest.raises(ToolError) as e:
        tools["web.fetch"].run({"url": site.base + "/to-local"})
    assert e.value.key == "tool.web_redirect_local" and "10.1.2.3" in str(e.value)


def test_fetch_refuses_names_that_resolve_to_local_addresses(tmp_path, monkeypatch):
    tools, hctx, _ = hands(tmp_path)
    monkeypatch.setattr(web, "_proxied", lambda url: False)
    monkeypatch.setattr(web, "_resolves_local", lambda h: True)          # 공개 이름이 내부 주소로 풀림 (DNS rebinding)
    with pytest.raises(ToolError) as e:
        tools["web.fetch"].run({"url": "https://rebind.example/x"})
    assert e.value.key == "tool.web_local_dns"


def test_fetch_skips_files_and_reports_errors(site, tmp_path):
    tools, _, _ = hands(tmp_path)
    out = tools["web.fetch"].run({"url": site.base + "/image.png"})
    assert "HTTP 200 · other" in out and "image/png" in out and "not downloaded" in out
    out = tools["web.fetch"].run({"url": site.base + "/missing"})
    assert "HTTP 404" in out and "The page was not found." in out
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "", "http://example.com:http/"):
        with pytest.raises(ToolError) as e:
            tools["web.fetch"].run({"url": bad})
        assert e.value.key == "tool.web_url"


def test_normalize_url():
    assert normalize_url("arxiv.org/abs/2610.00001") == "https://arxiv.org/abs/2610.00001"
    assert normalize_url("<https://example.com/a>") == "https://example.com/a"
    assert normalize_url("//example.com/a") == "https://example.com/a"
    with pytest.raises(ToolError):
        normalize_url("not a url")


# ---------------------------------------------------------------------- 글 뽑기
def test_html_text_keeps_pages_wrapped_in_one_form():
    """ASP.NET처럼 페이지 전체가 <form> 하나에 들어 있거나 바깥 틀의 class가 메뉴처럼 보여도 본문은 남는다."""
    body = "<p>" + "연구실 공지입니다. " * 60 + "</p>"
    title, text, _ = html_text(f"<html><body><form id='form1'><div class='wrap has-sidebar'>{body}"
                               f"<input name='q'><button>검색</button></div></form></body></html>")
    assert text.count("연구실 공지입니다.") == 60 and "검색" not in text


def test_xml_text_reads_atom_entries():
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>arXiv query</title>
    <entry><title>Paper C</title><id>http://arxiv.org/abs/2611.00003v1</id><published>2026-11-09T18:00:00Z</published>
    <summary>Summary C.</summary></entry></feed>"""
    title, text = xml_text(atom)
    assert title == "arXiv query"
    assert text.splitlines() == ["- Paper C", "  2026-11-09T18:00:00Z · http://arxiv.org/abs/2611.00003v1",
                                 "  Summary C."]


def test_egress_is_thread_safe_and_per_task(tmp_path):
    eg = Egress(tmp_path / "logs" / "egress.jsonl")
    threads = [threading.Thread(target=lambda i=i: eg.record("web.fetch", "a.org", f"https://a.org/{i}", task="T2"))
               for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    eg.record("web.search", "api.tavily.com", "q", "Tavily", "T3")
    assert eg.counts() == (21, 1) and len(eg.for_task("T2")) == 20 and eg.recent(1)[0].task == "T3"
    assert len((tmp_path / "logs" / "egress.jsonl").read_text(encoding="utf-8").splitlines()) == 21
