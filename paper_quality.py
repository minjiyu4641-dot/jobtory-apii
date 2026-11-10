"""논문의 중복·발행 시점·초록·출처 링크를 검사하는 규칙.

내용의 관련성은 research_service의 별도 초록 검토 단계에서 판단합니다.
이 규칙이나 관련성 검토 결과는 논문의 학술적 품질 보증이 아닙니다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from paper_service import Paper, paper_links


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _identity_text(value: str) -> str:
    return "".join(c for c in normalized_text(value).casefold() if c.isalnum())


def canonical_doi(value: str) -> str:
    value = value.strip()
    if value.casefold().startswith(("https://doi.org/", "http://doi.org/",
                                    "https://dx.doi.org/", "http://dx.doi.org/")):
        value = unquote(urlsplit(value).path.lstrip("/"))
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I)
    return value.casefold() if re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", value) else ""


def publication_year(paper: Paper) -> int | None:
    found = re.search(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", paper.publication_year)
    return int(found.group()) if found else None


def _same_paper(left: Paper, right: Paper) -> bool:
    left_doi, right_doi = canonical_doi(left.doi), canonical_doi(right.doi)
    if left_doi and right_doi:
        return left_doi == right_doi  # 서로 다른 유효 DOI를 제목만으로 합치지 않습니다.
    if left.provider == right.provider and left.paper_id == right.paper_id:
        return True
    if not _identity_text(left.title) or _identity_text(left.title) != _identity_text(right.title):
        return False
    if publication_year(left) is None or publication_year(left) != publication_year(right):
        return False
    left_authors = {_identity_text(a) for a in left.authors if _identity_text(a)}
    right_authors = {_identity_text(a) for a in right.authors if _identity_text(a)}
    return bool(left_authors & right_authors)


def deduplicate_papers(papers: list[Paper]) -> list[Paper]:
    """확인 가능한 동일 자료만 병합하고, 초록·링크가 풍부한 레코드를 보존합니다."""
    output: list[Paper] = []
    for paper in papers:
        found = next((i for i, old in enumerate(output) if _same_paper(old, paper)), None)
        if found is None:
            output.append(paper)
            continue
        old = output[found]
        best, other = sorted((old, paper), key=lambda p: (len(p.abstract.strip()), len(paper_links(p))), reverse=True)
        output[found] = replace(
            best,
            doi=canonical_doi(best.doi) or canonical_doi(other.doi),
            url=best.url or other.url, doc_url=best.doc_url or other.doc_url,
            journal=best.journal or other.journal,
            authors=tuple(dict.fromkeys((*best.authors, *other.authors))),
            keywords=tuple(dict.fromkeys((*best.keywords, *other.keywords))),
        )
    return output


def research_scope(question: str) -> str:
    compact = _identity_text(question)
    # 최근 상황과 옛 자료를 혼동하지 않도록 최근 시점 요구를 먼저 적용합니다.
    if (re.search(r"(?:최근|최신|요즘|요새).{0,8}(?:기술|연구|산업|논문|동향|추세)", compact) or
        any(term in compact for term in ("기술동향", "산업동향", "연구동향", "기술추세", "연구추세", "latest", "recent", "trend"))):
        return "recent"
    if any(term in compact for term in ("역사", "변천", "발전과정", "연혁", "태동", "초기연구", "과거연구", "과거의연구", "고전", "당시")):
        return "historical"
    return "background"


def policy_description(question: str, current_year: int | None = None) -> str:
    year = current_year or datetime.now(timezone.utc).year
    scope = research_scope(question)
    period = (f"최근 동향: {year - 4}–{year}년" if scope == "recent" else
              "역사적 배경: 발행 시점 제한 없음" if scope == "historical" else
              f"일반 기술·사업 배경: {year - 9}–{year}년")
    return f"{period}. 초록과 제공처 링크 또는 유효한 DOI가 있는 자료를 중복 제거한 뒤, 질문·공시와의 관련성을 검토합니다."


def exclusion_reason(paper: Paper, question: str, current_year: int | None = None) -> str:
    year = current_year or datetime.now(timezone.utc).year
    if not paper.abstract.strip():
        return "초록 미제공"
    if not paper_links(paper):
        return "출처 링크·DOI 미제공"
    published = publication_year(paper)
    if published is None:
        return "발행연도 미확인"
    if published > year:
        return "미래 발행연도"
    scope = research_scope(question)
    minimum = year - (4 if scope == "recent" else 9)
    if scope != "historical" and published < minimum:
        return "질문에 필요한 발행 시점 범위 밖"
    return ""


def eligible_papers(papers: list[Paper], question: str, current_year: int | None = None) -> tuple[list[Paper], dict[str, int]]:
    unique = deduplicate_papers(papers)
    counts: dict[str, int] = {}
    if len(papers) > len(unique):
        counts["중복 자료 통합"] = len(papers) - len(unique)
    selected = []
    for paper in unique:
        reason = exclusion_reason(paper, question, current_year)
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
        else:
            selected.append(paper)
    return selected, counts
