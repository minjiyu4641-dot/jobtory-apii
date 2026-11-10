"""국회도서관 국가학술정보(LOSI) 검색·상세조회 및 초록 근거 변환.

실행: python paper_service.py --query "스마트팩토리" --type ARTICLE --count 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import load_dotenv

# 공식 안내의 요청 주소입니다. HTTPS 요청은 확인 시 404를 반환했습니다.
# 인증키가 다른 호스트로 전달되지 않도록 주소를 고정하고 리디렉션을 차단합니다.
NAL_API_BASE = "http://losi-api.nanet.go.kr"
ROOT = Path(__file__).resolve().parent
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
PROVIDER = "국회도서관 LOSI"


class PaperError(ValueError):
    pass


@dataclass(frozen=True)
class Paper:
    paper_id: str
    title: str
    authors: tuple[str, ...]
    journal: str
    publication_year: str
    abstract: str
    doi: str
    url: str
    open_access_flag: str
    query: str
    retrieved_at: str
    provider: str = PROVIDER
    source_scope: str = "abstract"
    div_flag: str = ""
    keywords: tuple[str, ...] = ()
    doc_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PaperEvidence:
    source_id: str
    paper_id: str
    title: str
    authors: tuple[str, ...]
    journal: str
    publication_year: str
    doi: str
    url: str
    text: str
    is_truncated: bool
    source_type: str = "paper"
    source_scope: str = "abstract"
    provider: str = PROVIDER
    doc_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")


def _plain(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return ""
    parser = _TextOnly()
    parser.feed(value)
    return "".join(parser.parts).strip()


def _names(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(text for item in value
                               if (text := _plain(item.get("name") if isinstance(item, dict) else item))))


def safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password:
            return url.strip()
    except ValueError:
        pass
    return ""


def paper_links(paper: Paper | PaperEvidence) -> list[tuple[str, str]]:
    """제공된 URL과 검증한 DOI만 사용합니다. 상세보기 주소를 추측하지 않습니다."""
    links = []
    for label, value in (("논문 제공 페이지 열기", paper.url), ("원문 제공처 열기", paper.doc_url)):
        url = safe_url(value)
        if url and url not in {u for _, u in links}:
            links.append((label, url))
    doi = paper.doi.strip()
    if doi.casefold().startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")):
        doi = unquote(urlsplit(doi).path.lstrip("/"))
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)
    if re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", doi):
        url = "https://doi.org/" + quote(doi, safe="/()")
        if url not in {u for _, u in links}:
            links.append(("DOI로 논문 찾기", url))
    return links


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_api_message(value, secrets=()) -> str:
    message = " ".join(_plain(value).split())
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[인증정보 생략]")
    # 제공처가 인증키나 요청 주소를 오류에 되돌려 보내도 화면에 노출하지 않습니다.
    message = re.sub(r"https?://\S+", "[요청 주소 생략]", message, flags=re.I)
    message = re.sub(r"(?i)\b(?:authkey|api[_-]?key|servicekey|인증키)\s*[:=]\s*[^\s,;]+",
                     "인증키=[생략]", message)
    message = re.sub(r"\b[a-fA-F0-9]{24,}\b", "[인증정보 생략]", message)
    return message[:300]


def _no_search_results(error, stage: str) -> bool:
    if stage != "통합검색" or not isinstance(error, dict):
        return False
    # 코드 201 자체의 의미를 추정하지 않고, 실제 안내가 검색 결과 없음일 때만 0건으로 처리합니다.
    if str(error.get("code", "")) != "201":
        return False
    text = re.sub(r"\s+", "", _plain(error.get("message")))
    # 실제 LOSI 응답: "요청에 대한 정상 처리이지만, 결과값이 없습니다."
    if any(phrase in text for phrase in ("정상처리이지만", "정상처리이나", "정상처리되었지만")) and "결과값이없" in text:
        return True
    return any(phrase in text for phrase in (
        "검색결과가없", "검색결과없", "검색된결과가없", "조회결과가없",
        "검색된데이터가없", "검색결과가존재하지않", "조회결과가존재하지않",
    ))


def _result_nodes(payload, stage: str, secrets=()) -> list[dict]:
    """문서의 result 배열과 객체 응답을 처리하고 API 오류를 0건과 구분합니다."""
    if not isinstance(payload, dict):
        raise PaperError(f"국회도서관 {stage} 응답 형식이 다릅니다.")
    result = payload.get("result", payload)
    nodes = result if isinstance(result, list) else [result]
    if not nodes or any(not isinstance(n, dict) for n in nodes):
        raise PaperError(f"국회도서관 {stage} 응답 형식이 다릅니다.")
    for node in [payload, *nodes]:
        if node.get("error"):
            errors = node["error"]
            error = errors[0] if isinstance(errors, list) and errors else errors
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            if _no_search_results(error, stage):
                return [{"totalCount": 0, "searchList": []}]
            suffix = f"(코드 {code})" if re.fullmatch(r"[0-9]{1,4}", code) else ""
            detail = _safe_api_message(error.get("message") if isinstance(error, dict) else "", secrets)
            detail = detail or "제공처에서 상세 사유를 보내지 않았습니다."
            hint = " 인증키와 해당 API의 활용 승인 상태를 확인하세요." if code == "300" else ""
            raise PaperError(f"국회도서관 {stage} 응답{suffix}: {detail}{hint}")
    return nodes


def _post_losi(operation: str, params: dict, opener=None):
    stage = "통합검색" if operation == "searchTotal" else "상세조회"
    request = Request(NAL_API_BASE + "/" + operation,
                      data=urlencode(params).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                               "Accept": "application/json"})
    try:
        open_request = opener or build_opener(_NoRedirect()).open
        with open_request(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise PaperError(f"국회도서관 {stage} 요청에 실패했습니다(HTTP {exc.code}). "
                         "API 주소·인증키·활용 승인 상태를 확인하세요.") from None
    except (URLError, TimeoutError, OSError):
        raise PaperError(f"국회도서관 {stage}에 연결하지 못했습니다. 연결 상태를 확인해 다시 시도하세요.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PaperError("국회도서관 응답이 너무 큽니다. 검색 조건을 좁혀 주세요.")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise PaperError(f"국회도서관 {stage}에서 정상적인 JSON을 받지 못했습니다.") from None
    _result_nodes(payload, stage, secrets=(params.get("authKey", ""),))
    return payload


def _search_rows(payload) -> list[dict]:
    nodes = _result_nodes(payload, "통합검색")
    rows = next((n["searchList"] for n in nodes if "searchList" in n), None)
    total = next((str(n["totalCount"]) for n in nodes if "totalCount" in n), "")
    if total == "0" and (rows is None or rows == []):
        return []
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        raise PaperError("국회도서관 검색 목록을 읽지 못했습니다. 통합검색 응답 형식을 확인해 주세요.")
    return rows


def parse_nal_detail(payload, summary: dict, query: str, div_flag: str) -> Paper:
    nodes = _result_nodes(payload, "상세조회")
    detail = next((n["searchView"] for n in nodes if "searchView" in n), None)
    if not isinstance(detail, dict) or not detail:
        raise PaperError("국회도서관 상세정보가 없습니다. 상세보기 활용 권한과 응답 형식을 확인하세요.")
    lod_id = _plain(summary.get("lodID"))
    returned_id = _plain(detail.get("lodID"))
    returned_type = _plain(detail.get("divFlag"))
    if (returned_id and returned_id != lod_id) or (returned_type and returned_type != div_flag):
        raise PaperError("국회도서관 검색 목록과 상세정보의 논문 ID·자료유형이 다릅니다.")
    title = _plain(detail.get("title")) or _plain(summary.get("title"))
    if not lod_id or not title:
        raise PaperError("국회도서관 논문의 ID·제목을 읽지 못했습니다.")
    journal = summary.get("journal")
    journal_title = journal.get("title") if isinstance(journal, dict) else ""
    return Paper(
        paper_id=f"LOSI:{div_flag}:{lod_id}", title=title,
        authors=_names(detail.get("author")) or _names(summary.get("authorList")),
        journal=_plain(detail.get("journalName")) or _plain(journal_title),
        publication_year=_plain(detail.get("pubYear")) or _plain(summary.get("pubYear")),
        # 목차(toc)·주제어·제목으로 초록을 대신 만들지 않습니다.
        abstract=_plain(detail.get("abst")), doi=_plain(detail.get("doi")),
        url=safe_url(_plain(summary.get("url"))),
        open_access_flag="", query=query,
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        div_flag=div_flag, keywords=_names(detail.get("keyword")) or _names(summary.get("keywordList")),
        doc_url=safe_url(_plain(detail.get("docUrl"))),
    )


def search_nal(query: str, api_key: str, count: int = 5, div_flag: str = "ARTICLE",
               opener=None, progress: Callable[[int, int], None] | None = None,
               detail_cache: dict[tuple[str, str], Paper] | None = None, requester=None) -> list[Paper]:
    if not query.strip() or len(query) > 100:
        raise PaperError("논문 검색어는 1~100자로 입력하세요.")
    if not api_key.strip() or api_key.strip() == "YOUR_NAL_API_KEY":
        raise PaperError("국회도서관 국가학술정보 API 키를 입력해 주세요.")
    if not isinstance(count, int) or not 1 <= count <= 10:
        raise PaperError("이 앱의 검색 건수는 1~10 사이여야 합니다.")
    if div_flag not in {"ARTICLE", "THESIS"}:
        raise PaperError("자료유형은 ARTICLE(학술기사) 또는 THESIS(학위논문)를 선택하세요.")
    post = requester or (lambda operation, params: _post_losi(operation, params, opener))
    payload = post("searchTotal", {"authKey": api_key.strip(), "searchTerm": query.strip(),
                         "searchRange": div_flag, "pageNo": 1, "printRowCnt": count})
    rows = _search_rows(payload)
    summaries = {}
    for row in rows:
        if _plain(row.get("divFlag")) not in {"", div_flag}:
            continue
        identity = _plain(row.get("lodID"))
        if not identity:
            raise PaperError("국회도서관 검색 결과에 논문 ID(lodID)가 없습니다.")
        summaries.setdefault(identity, row)
    output = []
    selected = list(summaries.values())[:count]
    for i, summary in enumerate(selected):
        if progress:
            progress(i, len(selected))
        cache_id = (div_flag, str(summary["lodID"]))
        if detail_cache is not None and cache_id in detail_cache:
            paper = replace(detail_cache[cache_id], query=query.strip())
        else:
            detail = post("searchView", {"authKey": api_key.strip(), "lodID": summary["lodID"],
                                "divFlag": div_flag})
            paper = parse_nal_detail(detail, summary, query.strip(), div_flag)
            if detail_cache is not None:
                detail_cache[cache_id] = paper
        output.append(paper)
    if progress:
        progress(len(selected), len(selected))
    return output


def save_papers(papers: list[Paper], directory: Path | str) -> int:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    for paper in papers:
        name = hashlib.sha256(paper.paper_id.encode()).hexdigest()[:20] + ".json"
        dest = target / name
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(paper.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dest)
    return len(papers)


def load_papers(directory: Path | str) -> list[Paper]:
    output = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig"))
            names = ("paper_id", "title", "journal", "publication_year", "abstract", "doi", "url",
                     "open_access_flag", "query", "retrieved_at", "provider", "source_scope")
            if not isinstance(row, dict) or any(not isinstance(row.get(n), str) for n in names):
                raise ValueError("invalid record")
            if not row["paper_id"].strip() or not row["title"].strip() or row["source_scope"] != "abstract":
                raise ValueError("invalid scope")
            authors, keywords = row.get("authors"), row.get("keywords", [])
            if any(not isinstance(v, list) or any(not isinstance(a, str) for a in v) for v in (authors, keywords)):
                raise ValueError("invalid authors/keywords")
            if any(not isinstance(row.get(n, ""), str) for n in ("div_flag", "doc_url")):
                raise ValueError("invalid metadata")
            row["url"] = safe_url(row["url"])
            output.append(Paper(**{n: row[n] for n in names}, authors=tuple(authors),
                                keywords=tuple(keywords), div_flag=row.get("div_flag", ""),
                                doc_url=safe_url(row.get("doc_url", ""))))
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise PaperError(f"논문 JSON을 읽지 못했습니다: {path.name}") from exc
    return output


def make_paper_evidence(paper: Paper, max_chars: int = 4500) -> PaperEvidence:
    if not paper.abstract.strip():
        raise PaperError("초록이 없는 논문은 답변 근거로 선택할 수 없습니다.")
    seed = f"{paper.provider}|{paper.paper_id}|{paper.abstract}"
    return PaperEvidence(
        source_id="P" + hashlib.sha256(seed.encode()).hexdigest()[:10], paper_id=paper.paper_id,
        title=paper.title, authors=paper.authors, journal=paper.journal,
        publication_year=paper.publication_year, doi=paper.doi, url=paper.url,
        text=paper.abstract[:max_chars], is_truncated=len(paper.abstract) > max_chars,
        provider=paper.provider, doc_url=paper.doc_url,
    )


def main():
    parser = argparse.ArgumentParser(description="국회도서관 LOSI 논문 검색·상세조회로 초록 JSON을 저장합니다.")
    parser.add_argument("--query", required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--type", choices=["ARTICLE", "THESIS"], default="ARTICLE", dest="div_flag")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    key = os.getenv("NAL_API_KEY", "").strip() or getpass("국회도서관 국가학술정보 API 키 입력: ").strip()
    try:
        papers = search_nal(args.query, key, args.count, args.div_flag,
                            progress=lambda done, total: print(f"상세조회 {done}/{total}"))
        save_papers(papers, ROOT / "papers")
    except (PaperError, OSError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    print(f"검색 {len(papers)}건 저장 완료 · 초록 있음 {sum(bool(p.abstract.strip()) for p in papers)}건")
    for paper in papers:
        print(f"- {paper.title} ({paper.publication_year or '연도 미제공'})")


if __name__ == "__main__":
    main()
