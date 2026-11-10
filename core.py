"""회사별 JSON 로딩과 근거 검색. Streamlit과 분리한 순수 Python 코드입니다."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


CATEGORY_LABELS = {
    "business": "사업의 개요", "products": "주요 제품 및 서비스",
    "revenue_orders": "매출 및 수주상황", "financial_risk": "위험관리 및 파생거래",
    "financial_notes": "재무제표 주석", "management": "경영진단 및 분석의견",
    "audit": "회계감사인의 감사의견", "research_development": "연구개발활동",
    "employees": "직원 현황", "financial_statements": "재무제표 본표",
    "consolidated_notes": "연결재무제표 주석",
}


@dataclass(frozen=True)
class Question:
    id: str
    label: str
    text: str
    categories: tuple[str, ...]
    terms: tuple[str, ...]


# 화면의 카테고리와 질문을 한곳에서 관리합니다. 기업 자료의 category와는 별개입니다.
# 각 카테고리는 요청한 질문 3개를 제공하고, 선택 시 첫 질문을 입력창에 넣습니다.
QUESTION_GROUPS = (('business',
  '업무',
  '실제로 어떤 일을 하는 회사인가?',
  (('주요 사업·제품·고객과, 각 사업이 어떤 일을 하는지 설명해줘.',
    ('business', 'products', 'revenue_orders'),
    ('사업', '제품', '서비스', '고객', '솔루션')),
   ('모회사와 자회사가 수행하는 사업을 구분하고, 지원할 때 고용 법인을 확인해야 하는 이유를 설명해줘.',
    ('business', 'products', 'consolidated_notes', 'revenue_orders'),
    ('모회사', '자회사', '법인', '종속', '수행')),
   ('사업별로 직접 개발·제조하는 부분과 외부 제품 공급·운영을 담당하는 부분을 구분해 설명해줘.',
    ('products', 'business', 'revenue_orders', 'research_development'),
    ('개발', '제조', '공급', '운영', '구축', '유지보수')))),
 ('technology',
  '성장',
  '회사와 함께 어떤 분야의 경력을 쌓을 수 있을까?',
  (('회사가 제시하는 시장 성장 전망과 경쟁력, 이를 뒷받침하는 공시 근거를 설명해줘.',
    ('business', 'management', 'products'),
    ('시장', '성장', '전망', '경쟁', '점유율', '수요')),
   ('신사업이 계획·투자·개발·상용화·매출 발생 중 어느 단계이며, 실제 성과가 무엇인지 설명해줘.',
    ('research_development', 'business', 'products', 'management', 'revenue_orders'),
    ('신사업', '신규', '투자', '개발', '상용화', '출시', '매출')),
   ('경력 방향을 검토할 수 있도록, 회사의 연구개발·투자가 집중된 사업과 기술 분야를 설명해줘.',
    ('research_development', 'management', 'business', 'products'),
    ('연구개발', '투자', '개발', '기술', '연구', '과제')))),
 ('performance',
  '실적',
  '사업이 실제 성과로 이어지고 있을까?',
  (('최근 매출과 영업이익이 어떻게 달라졌고, 어떤 사업이 변화에 영향을 줬는지 회사의 설명을 바탕으로 설명해줘.',
    ('management', 'financial_statements', 'revenue_orders', 'products'),
    ('매출액', '영업이익', '영업손실', '증감', '감소', '증가', '원인')),
   ('매출이 유지보수·구독·운영처럼 반복적으로 발생하는지, 개별 프로젝트나 일회성 거래에 의존하는지 설명해줘.',
    ('revenue_orders', 'products', 'business', 'consolidated_notes'),
    ('유지보수', '구독', '운영', '반복', '프로젝트', '계약', '매출')),
   ('수주잔고와 주요 계약의 기간·진행 상황을 바탕으로, 앞으로의 일감과 매출을 어디까지 확인할 수 있는지 설명해줘.',
    ('revenue_orders', 'management', 'business', 'consolidated_notes'),
    ('수주잔고', '수주잔액', '계약', '기간', '진행', '납기', '수익인식')))),
 ('stability',
  '안정성',
  '회사가 지속적으로 운영될 기반이 있을까?',
  (('이익이 실제 현금 유입으로 이어지는지와, 차입금·감사의견에서 확인할 주의사항을 설명해줘.',
    ('financial_statements', 'financial_notes', 'consolidated_notes', 'audit', 'management', 'financial_risk'),
    ('영업활동', '현금흐름', '매출채권', '차입금', '감사의견', '유동성', '불확실성')),
   ('특정 고객이나 그룹 계열사에 대한 매출 의존도를 설명해줘.',
    ('revenue_orders', 'business', 'consolidated_notes', 'products'),
    ('주요고객', '매출처', '계열사', '특수관계자', '의존', '고객', '비중')),
   ('고객 투자·정부 정책·원가·환율 등이 사업에 미치는 영향과 회사의 대응을 설명해줘.',
    ('financial_risk', 'management', 'business', 'revenue_orders', 'financial_notes'),
    ('고객', '투자', '정책', '원가', '환율', '위험', '대응')))),
 ('salary',
  '연봉',
  '공시된 급여를 어떻게 읽어야 할까?',
  (('직원 1인 평균급여와 해당 금액의 기준 연도·집계 대상을 설명해줘.', ('employees',), ('1인평균', '평균급여', '급여액', '직원', '기준일', '집계')),
   ('공시된 사업부문별 평균급여를 비교하고, 차이를 해석할 때 함께 확인해야 할 정보를 설명해줘.', ('employees',), ('사업부문', '급여', '평균', '근속', '산정')),
   ('평균급여의 산정 기준과 상여·성과급·임원 포함 여부를 확인하고, 신입 초봉으로 해석할 때의 주의점을 설명해줘.',
    ('employees',),
    ('산정', '상여', '성과급', '임원', '평균급여', '급여', '주석')))),
 ('work_environment',
  '근무환경',
  '어떤 고용·근무 조건을 확인할 수 있을까?',
  (('시차출퇴근·선택근무·원격근무 제도의 도입 및 사용 현황을 설명해줘.', ('employees',), ('유연근무', '시차출퇴근', '선택근무', '원격근무', '사용', '활용')),
   ('육아휴직·육아기 단축근무·배우자 출산휴가의 사용 현황을 설명해줘.', ('employees',), ('육아휴직', '육아기', '단축근무', '출산휴가', '사용률', '사용자')),
   ('직원 수·고용형태·평균근속연수를 설명하고, 오래 다닐 회사를 판단할 때 이 정보만으로 알기 어려운 부분을 설명해줘.',
    ('employees',),
    ('직원', '고용형태', '기간제', '근속', '정함', '산정')))))

QUESTIONS = tuple(Question(key, label, rows[0][0], rows[0][1], rows[0][2])
                  for key, label, description, rows in QUESTION_GROUPS)
QUESTION_BY_ID = {q.id: q for q in QUESTIONS}
QUESTION_CATEGORY_DESCRIPTIONS = {key: description for key, label, description, rows in QUESTION_GROUPS}
FOLLOW_UP_QUESTIONS = {key: tuple(row[0] for row in rows)
                       for key, label, description, rows in QUESTION_GROUPS}
# 후속 질문은 넓은 카테고리가 아닌 개별 질문에 맞는 보고서 절에서 검색합니다.
FOLLOW_UP_BY_TEXT = {text: Question(f"{key}_{number}", label, text, categories, terms)
                    for key, label, description, rows in QUESTION_GROUPS
                    for number, (text, categories, terms) in enumerate(rows)}

@dataclass(frozen=True)
class Section:
    corp_name: str
    fiscal_year: int
    section: str
    category: str
    source_file: str
    start_pdf_page: int
    end_pdf_page: int
    text: str


@dataclass(frozen=True)
class Evidence:
    source_id: str
    corp_name: str
    fiscal_year: int
    section: str
    category: str
    source_file: str
    start_pdf_page: int
    end_pdf_page: int
    text: str
    char_start: int
    char_end: int

    def to_dict(self) -> dict:
        return asdict(self)


class DataError(ValueError):
    pass


def load_reports(data_dir: str | Path) -> list[Section]:
    """잘못된 파일을 조용히 무시하지 않고 어떤 파일에서 실패했는지 알립니다."""
    paths = sorted(Path(data_dir).glob("*.json"))
    if not paths:
        raise DataError("data 폴더에 JSON 파일이 없습니다.")
    sections = []
    for path in paths:
        try:
            rows = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataError(f"JSON 파일을 읽을 수 없습니다: {path.name}") from exc
        if not isinstance(rows, list) or not rows:
            raise DataError(f"JSON 최상위는 비어 있지 않은 배열이어야 합니다: {path.name}")
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict):
                    raise TypeError("object required")
                names = ("corp_name", "section", "category", "source_file", "text")
                if any(not isinstance(row.get(k), str) or not row[k].strip() for k in names):
                    raise ValueError("required text missing")
                start, end = int(row["start_pdf_page"]), int(row["end_pdf_page"])
                year = int(row["fiscal_year"])
                if start < 1 or end < start or not 1900 <= year <= 2200:
                    raise ValueError("invalid source range")
                sections.append(Section(
                    **{k: row[k].strip() for k in names}, fiscal_year=year,
                    start_pdf_page=start, end_pdf_page=end,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise DataError(f"필수 항목을 확인하세요: {path.name}, 항목 {index + 1}") from exc
    return sections


def split_section(section: Section, size: int = 2400, overlap: int = 260) -> list[Evidence]:
    """원문 문자 위치를 보존합니다. 섹션 범위를 정확한 인용 페이지로 오인하지 않습니다."""
    if size <= overlap or overlap < 0:
        raise ValueError("size must exceed overlap")
    text, start, output = section.text, 0, []
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n\n", start + size // 2, end)
            if boundary < 0:
                boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        body = text[start:end]
        seed = f"{section.corp_name}|{section.fiscal_year}|{section.source_file}|{section.section}|{start}|{body}"
        output.append(Evidence(
            source_id="S" + hashlib.sha256(seed.encode()).hexdigest()[:10],
            corp_name=section.corp_name, fiscal_year=section.fiscal_year,
            section=section.section, category=section.category, source_file=section.source_file,
            start_pdf_page=section.start_pdf_page, end_pdf_page=section.end_pdf_page,
            text=body, char_start=start, char_end=end,
        ))
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return output


def build_index(sections: list[Section]) -> list[Evidence]:
    seen, result = set(), []
    for section in sections:
        for chunk in split_section(section):
            if chunk.source_id not in seen:
                seen.add(chunk.source_id)
                result.append(chunk)
    return result


def route_question(question: str, preset_id: str | None = None) -> Question:
    text = question.strip()
    if preset_id in QUESTION_BY_ID and text == QUESTION_BY_ID[preset_id].text:
        return QUESTION_BY_ID[preset_id]
    if text in FOLLOW_UP_BY_TEXT:
        return FOLLOW_UP_BY_TEXT[text]
    routes = (
        (("직원", "근속", "급여", "초봉", "연봉", "고용형태", "유연근무", "시차출퇴근", "선택근무",
          "원격근무", "재택근무", "육아휴직", "단축근무", "출산휴가"),
         ("employees",), ("직원", "근속", "급여", "유연근무", "육아", "사용")),
        (("감사", "소송", "차입", "위험", "리스크", "보증"),
         ("audit", "financial_risk", "management", "financial_notes", "consolidated_notes", "financial_statements"),
         ("감사의견", "불확실성", "소송", "차입", "유동성", "보증", "현금흐름")),
        (("자회사", "법인", "헬스케어"),
         ("business", "products", "revenue_orders", "consolidated_notes"),
         ("법인", "자회사", "헬스케어", "대상회사", "매출", "비중")),
        (("현금흐름", "영업이익", "실적", "수익성"),
         ("financial_statements", "management", "revenue_orders", "financial_notes", "consolidated_notes"),
         ("매출액", "영업이익", "영업활동", "현금흐름", "증감", "원인")),
        (("해외", "수출", "국내", "지역별"), ("revenue_orders", "business"), ("지역", "국내", "해외", "수출", "내수")),
        (("매출", "비중", "수주", "계약"), ("revenue_orders", "products", "business", "consolidated_notes"),
         ("매출", "비중", "수주잔고", "수주잔액", "계약", "연결조정")),
        (("고객", "판매", "입찰"), ("revenue_orders", "business", "products", "consolidated_notes"),
         ("판매", "고객", "매출처", "계열사", "특수관계자")),
        (("연구", "신사업", "기술", "AI", "개발", "투자"),
         ("research_development", "business", "products", "management"),
         ("연구개발", "신사업", "투자", "개발", "기술", "계획", "출시")),
    )
    for triggers, categories, terms in routes:
        if any(t.casefold() in text.casefold() for t in triggers):
            return Question("custom", "직접 입력", text, categories, terms)
    return Question("custom", "직접 입력", text, ("business", "products", "revenue_orders", "management"), ())


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[가-힣]{2,}|[a-zA-Z]{2,}", text.casefold()))
    # 한국어 조사와 어미 차이에 대응하는 작은 문자 토큰. 외부 임베딩 호출은 없습니다.
    for word in list(tokens):
        if re.fullmatch(r"[가-힣]+", word):
            tokens.update(word[i:i + 2] for i in range(len(word) - 1))
    return tokens


def retrieve(index: list[Evidence], corp_name: str, fiscal_year: int, question: str,
             preset_id: str | None = None, top_k: int = 7, max_chars: int = 18000,
             categories: tuple[str, ...] | None = None) -> list[Evidence]:
    if not question.strip() or len(question) > 2000:
        raise ValueError("질문은 1~2,000자로 입력하세요.")
    if top_k < 1:
        raise ValueError("top_k must be positive")
    route = route_question(question, preset_id)
    if categories is not None:
        route = Question("interest", "관심분야", question, categories, ())
    candidates = [c for c in index if c.corp_name == corp_name and c.fiscal_year == fiscal_year
                  and c.category in route.categories]
    if not candidates:
        return []
    qtokens = _tokens(question)
    token_sets = [_tokens(c.text) for c in candidates]
    df = Counter(token for tokens in token_sets for token in tokens)
    scores = {}
    for c, tokens in zip(candidates, token_sets):
        lexical = sum(math.log(1 + len(candidates) / (1 + df[t])) for t in qtokens & tokens)
        terms = sum(min(c.text.casefold().count(t.casefold()), 4) for t in route.terms)
        # 질문에 맞는 절을 우선하고, 본문과 질문의 어휘로 순위를 정합니다.
        priority = (len(route.categories) - route.categories.index(c.category)) * 0.7
        score = lexical + terms * 0.9 + priority
        if c.char_start == 0:
            score += 1.5
        scores[c.source_id] = score
    ranked = sorted(candidates, key=lambda c: (-scores[c.source_id], c.char_start))
    selected, used, total = [], set(), 0

    def add(chunk: Evidence) -> None:
        nonlocal total
        if chunk.source_id in used or total + len(chunk.text) > max_chars or len(selected) >= top_k:
            return
        # 같은 절의 거의 동일한 중첩 문단을 반복해서 보내지 않습니다.
        for previous in selected:
            if previous.section == chunk.section and previous.source_file == chunk.source_file:
                overlap = max(0, min(previous.char_end, chunk.char_end) - max(previous.char_start, chunk.char_start))
                if overlap > min(len(previous.text), len(chunk.text)) * 0.55:
                    return
        selected.append(chunk)
        used.add(chunk.source_id)
        total += len(chunk.text)

    # 회사 개요 또는 각 소스 종류의 근거가 한 종류의 긴 문서에 밀리지 않게 확보합니다.
    for category in route.categories:
        group = [c for c in ranked if c.category == category]
        if group:
            add(group[0])
    for chunk in ranked:
        add(chunk)
    return selected


def coverage_notes(sections: list[Section], corp_name: str, fiscal_year: int, question: str,
                   preset_id: str | None = None) -> list[str]:
    selected = [s for s in sections if s.corp_name == corp_name and s.fiscal_year == fiscal_year]
    categories = {s.category for s in selected}
    route = route_question(question, preset_id)
    notes = ["페이지는 JSON에 기록된 추출 절 전체의 범위입니다. 발췌문 한 문장의 정확한 페이지가 아닙니다."]
    if "research_development" in route.categories or "연구" in question or "신사업" in question:
        if "research_development" not in categories:
            notes.append("현재 JSON에는 연구개발활동 전용 항목이 없습니다. 사업 소개에 있는 정보만으로 설명하며, 연구개발이 없다고 단정할 수 없습니다.")
    if "employees" in route.categories and "employees" not in categories:
        notes.append("현재 JSON에 직원 현황 항목이 없습니다. 급여·고용형태·평균근속·유연근무·육아지원 자료와 산정 주석은 원문 PDF에서 추가 추출해야 합니다. 자료 미포함을 제도·활동이 없다는 뜻으로 해석하지 마세요.")
    if route.id == "performance" or any(t in question for t in ("실적", "현금흐름", "영업이익")):
        if "financial_statements" not in categories:
            notes.append("재무제표 본표 전용 항목이 없습니다. 경영진단·매출표에 실제 제시된 기간과 숫자만 사용합니다.")
    if "financial_notes" in route.categories and "consolidated_notes" not in categories:
        notes.append("현재 financial_notes는 '5. 재무제표 주석'입니다. 연결재무제표 주석으로 간주하지 않습니다.")
    return notes
