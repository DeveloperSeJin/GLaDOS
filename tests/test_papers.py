"""논문 찾기 시험 (M6.5): papers.search — OpenAlex + arXiv, 실패하면 Semantic Scholar.

진짜 API는 부르지 않는다: httpx를 가짜 응답(MockTransport)으로 바꾼다. 응답 모양은 각 API 문서 기준.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import check, make_places
from glados.config import WebConfig
from glados.hands import HandContext, ToolError
from glados.hands import papers, web
from glados.safety.policy import Policy

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2610.00001v2</id>
    <published>2026-10-02T17:00:00Z</published>
    <title>A Survey of LLM
      Evaluation Metrics</title>
    <summary>We survey 42 metrics for evaluating large language models.</summary>
    <author><name>Ada Lovelace</name></author><author><name>Alan Turing</name></author>
    <link href="http://arxiv.org/abs/2610.00001v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2610.00001v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2611.00009v1</id>
    <published>2026-11-05T09:00:00Z</published>
    <title>Judging the Judges Again</title>
    <summary>LLM-as-a-judge agrees with humans 81 percent of the time.</summary>
    <author><name>Grace Hopper</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2611.00009v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>"""

OPENALEX_WORKS = {
    "meta": {"count": 2},
    "results": [
        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/xyz", "display_name": "FactScore Revisited",
         "publication_year": 2025, "publication_date": "2025-06-01", "cited_by_count": 120,
         "authorships": [{"author": {"display_name": f"Author {i}"}} for i in range(5)],
         "primary_location": {"source": {"display_name": "ACL"}, "landing_page_url": "https://aclanthology.org/x",
                              "pdf_url": "https://aclanthology.org/x.pdf"},
         "abstract_inverted_index": {"Atomic": [0], "fact": [1], "scoring": [2], "for": [3], "long": [4], "answers.": [5]},
         "open_access": {"oa_url": "https://aclanthology.org/x.pdf"}},
        {"id": "https://openalex.org/W2", "doi": None, "display_name": "A survey of LLM evaluation metrics",
         "publication_year": 2026, "publication_date": "2026-10-02", "cited_by_count": 3,
         "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
         "primary_location": {"source": {"display_name": "arXiv (Cornell University)"},
                              "landing_page_url": "https://arxiv.org/abs/2610.00001"},
         "abstract_inverted_index": None, "open_access": {"oa_url": None}},
    ],
}

S2_RESULT = {"total": 1, "offset": 0, "data": [
    {"paperId": "abc", "title": "Judging the Judges", "year": 2024, "publicationDate": "2024-09-01",
     "authors": [{"authorId": "1", "name": "Grace Hopper"}], "venue": "NeurIPS", "citationCount": 77,
     "externalIds": {"ArXiv": "2409.12345", "DOI": "10.5555/judge"}, "url": "https://www.semanticscholar.org/paper/abc",
     "abstract": "Judges agree.", "openAccessPdf": {"url": "https://arxiv.org/pdf/2409.12345", "status": "GREEN"}},
]}


class FakeAPIs:
    def __init__(self, openalex_status: int = 200, s2_status: int = 200):
        self.requests: list[httpx.Request] = []
        self.openalex_status = openalex_status
        self.s2_status = s2_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "api.openalex.org":
            if self.openalex_status != 200:
                return httpx.Response(self.openalex_status, json={"error": "limit"})
            return httpx.Response(200, json=OPENALEX_WORKS)
        if host == "export.arxiv.org":
            return httpx.Response(200, content=ATOM.encode(), headers={"content-type": "application/atom+xml"})
        if host == "api.semanticscholar.org":
            if self.s2_status != 200:
                return httpx.Response(self.s2_status, json={"message": "Too Many Requests"})
            return httpx.Response(200, json=S2_RESULT)
        return httpx.Response(404)

    def params(self, host: str) -> list[dict[str, list[str]]]:
        return [parse_qs(urlsplit(str(r.url)).query) for r in self.requests if r.url.host == host]


@pytest.fixture
def apis(monkeypatch):
    fake = FakeAPIs()
    real = httpx.Client

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(fake)
        kw["trust_env"] = False
        return real(*args, **kw)

    monkeypatch.setattr(papers.httpx, "Client", client)
    monkeypatch.setattr(papers, "ARXIV_GAP_S", 0.0)
    return fake


def tool(tmp_path: Path, env: dict[str, str] | None = None):
    events = []
    hctx = HandContext(places=make_places("linux"), home=tmp_path, web=WebConfig(), env=env or {},
                       emit=events.append, task_id="T1")
    tools = {t.name: t for t in web.web_tools(hctx)}
    return tools["papers.search"], hctx, events


def test_relevance_merges_openalex_and_arxiv(apis, tmp_path):
    search, hctx, events = tool(tmp_path)
    out = search.run({"query": "LLM evaluation metrics"})
    lines = out.splitlines()
    assert lines[0] == 'Paper search (OpenAlex, arXiv): "LLM evaluation metrics" · relevance · 3 papers'
    # OpenAlex 1번, arXiv 1번, OpenAlex 2번(= arXiv 1번, 하나로), arXiv 2번 차례
    assert "- FactScore Revisited | https://doi.org/10.1000/xyz" in out
    assert "2025-06-01 · ACL · Author 0, Author 1, Author 2 et al. · cited 120 · PDF https://aclanthology.org/x.pdf" in out
    assert "  Atomic fact scoring for long answers." in out                          # 초록을 다시 이음
    assert out.count("https://arxiv.org/abs/2610.00001") == 1                         # 두 곳에서 온 같은 논문은 하나로
    survey = next(i for i, ln in enumerate(lines) if "Survey of LLM" in ln)
    assert lines[survey].endswith("| https://arxiv.org/abs/2610.00001")
    assert "cited 3" in lines[survey + 1] and "PDF https://arxiv.org/pdf/2610.00001v2" in lines[survey + 1]
    assert "We survey 42 metrics" in lines[survey + 2]                                # 빈 초록은 arXiv에서 채움
    assert "- Judging the Judges Again | https://arxiv.org/abs/2611.00009" in out
    assert out.endswith("Cite the URLs exactly as shown.") and "not instructions" in out
    assert [s.host for s in hctx.egress.items] == ["api.openalex.org", "export.arxiv.org"]
    assert hctx.egress.counts() == (2, 2)                                             # 머리글 칩의 "웹 검색"에 든다
    assert [e.data["tool"] for e in events] == ["papers.search", "papers.search"]
    oa = apis.params("api.openalex.org")[0]
    assert oa["search"] == ["LLM evaluation metrics"] and "sort" not in oa and "api_key" not in oa
    ax = apis.params("export.arxiv.org")[0]
    assert ax["search_query"] == ["all:LLM AND all:evaluation AND all:metrics"] and ax["sortBy"] == ["relevance"]
    # 같은 검색은 한 시간 동안 다시 보내지 않는다
    again = search.run({"query": "llm evaluation metrics"})
    assert again.endswith("nothing was sent again.)") and len(apis.requests) == 2


def test_recent_and_since(apis, tmp_path):
    search, _, _ = tool(tmp_path, {"OPENALEX_API_KEY": "oa-key"})
    out = search.run({"query": "LLM evaluation", "sort": "recent", "since": 2025, "max_results": 3})
    oa = apis.params("api.openalex.org")[0]
    assert oa["sort"] == ["publication_date:desc"] and oa["filter"] == ["from_publication_date:2025-01-01"]
    assert oa["api_key"] == ["oa-key"] and oa["per_page"] == ["3"]
    ax = apis.params("export.arxiv.org")[0]
    assert ax["sortBy"] == ["submittedDate"] and ax["sortOrder"] == ["descending"]
    assert ax["search_query"] == ["all:LLM AND all:evaluation AND submittedDate:[202501010000 TO 299912312359]"]
    titles = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert titles[0].startswith("- Judging the Judges Again") and "since 2025" in out.splitlines()[0]


def test_cited_skips_arxiv(apis, tmp_path):
    search, hctx, _ = tool(tmp_path)
    out = search.run({"query": "fact scoring", "sort": "cited"})
    assert [r.url.host for r in apis.requests] == ["api.openalex.org"]
    assert apis.params("api.openalex.org")[0]["sort"] == ["cited_by_count:desc"]
    titles = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert titles[0].startswith("- FactScore Revisited") and len(hctx.egress.items) == 1


def test_openalex_limit_falls_back_to_semantic_scholar(apis, tmp_path):
    apis.openalex_status = 429
    search, hctx, _ = tool(tmp_path, {"SEMANTIC_SCHOLAR_API_KEY": "s2-key"})
    out = search.run({"query": "LLM judge"})
    assert out.splitlines()[0].startswith('Paper search (Semantic Scholar, arXiv): "LLM judge"')
    assert "- Judging the Judges | https://arxiv.org/abs/2409.12345" in out and "NeurIPS" in out and "cited 77" in out
    assert "OpenAlex did not answer: OpenAlex rate limit reached (HTTP 429)" in out
    s2 = next(r for r in apis.requests if r.url.host == "api.semanticscholar.org")
    assert s2.headers["x-api-key"] == "s2-key"
    q = parse_qs(urlsplit(str(s2.url)).query)
    assert q["query"] == ["LLM judge"] and "externalIds" in q["fields"][0]
    assert [s.host for s in hctx.egress.items] == ["api.openalex.org", "export.arxiv.org", "api.semanticscholar.org"]


def test_every_source_failing_is_one_clear_error(apis, tmp_path):
    apis.openalex_status = 429
    apis.s2_status = 429
    search, _, _ = tool(tmp_path)
    with pytest.raises(ToolError) as e:
        search.run({"query": "x y", "sort": "cited"})                                 # arXiv는 안 부름
    assert e.value.key == "tool.papers_limit"
    for bad, key in (({"query": " "}, "tool.web_query"), ({"query": "x", "sort": "newest"}, "tool.papers_sort"),
                     ({"query": "x", "since": 1700}, "tool.papers_since"), ({"query": "x", "since": "soon"}, "tool.not_number")):
        with pytest.raises(ToolError) as e:
            search.run(bad)
        assert e.value.key == key


def test_arxiv_calls_are_three_seconds_apart(monkeypatch):
    waits = []
    clock = {"now": 100.0}
    monkeypatch.setattr(papers.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(papers.time, "sleep", lambda s: waits.append(round(s, 2)))
    monkeypatch.setattr(papers, "_arxiv_last", [0.0])
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=ATOM.encode()))
    with httpx.Client(transport=transport) as c:
        papers.arxiv(c, "a", 2, "relevance", None)
        clock["now"] += 1.0
        papers.arxiv(c, "b", 2, "relevance", None)
    assert waits == [2.0]


def test_parse_arxiv_ignores_error_entries_and_bad_xml():
    error_feed = ATOM.split("<entry>")[0] + "<entry><id>http://arxiv.org/api/errors#bad</id><title>Error</title></entry></feed>"
    assert papers.parse_arxiv(error_feed.encode()) == []
    with pytest.raises(ToolError):
        papers.parse_arxiv(b"")


def test_policy_checks_paper_queries_like_web_queries():
    p = Policy(__import__("conftest").base_layers())
    assert check(p, "linux", "papers.search", query="LLM evaluation metrics").verdict == "allow"
    d = check(p, "linux", "papers.search", query="papers by gildong@example.com")
    assert (d.verdict, d.rule, d.grantable) == ("ask", "코드:개인정보", False)


def test_app_shows_what_was_sent(apis, tmp_path):
    """앱에서: 화면의 ↗ 줄과 /sent에 논문 검색어가 보이고, 모델에게 결과가 간다."""
    import io

    from conftest import FakeLLM, act, answer
    from glados import cli
    from glados.config import ensure_home, load_config

    home = tmp_path / "home"
    ensure_home(home)
    out = io.StringIO()
    a = cli.App(load_config(home), cli.Screen(ansi=False, out=out),
                llm=FakeLLM([act("papers.search", query="LLM evaluation metrics", sort="recent"),
                             answer("Newest: Judging the Judges Again (2026-11-05).", "Judges, judged.",
                                    ["https://arxiv.org/abs/2611.00009"])]), os_name="linux")
    try:
        result = a.ask_task("what are the newest papers on LLM evaluation metrics")
        assert result.status == "done" and result.sources == ["https://arxiv.org/abs/2611.00009"]
        text = out.getvalue()
        assert '↗ 외부 전송 · api.openalex.org · 검색어 "LLM evaluation metrics (recent)"' in text
        assert '↗ 외부 전송 · export.arxiv.org · 검색어 "LLM evaluation metrics (recent)"' in text
        assert "Paper search (OpenAlex, arXiv)" in a.llm.calls[-1][0][-1]["content"]
        mark = len(out.getvalue())
        a.command("/sent")
        assert "api.openalex.org" in out.getvalue()[mark:] and "export.arxiv.org" in out.getvalue()[mark:]
    finally:
        a.close()
