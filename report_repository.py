"""Read-only Supabase report retrieval for the five supported companies.

No DDL, writes, arbitrary SQL, embeddings or external DART requests are used.
The selected filing and the original table context travel with every excerpt.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from core import Evidence, Section, coverage_notes, retrieve, route_question, split_section
from paper_repository import PaperRepository

# DART corporation codes, verified against the project's companies.csv.
# Do not select by a fuzzy company-name match or substitute another company.
DB_COMPANIES = {
    '이삭엔지니어링': '01135640',
    '롯데이노베이트': '01262032',
    '인성정보': '00229021',
    '링네트': '00352499',
    '에스트래픽': '01014055',
}
SECTION_CATEGORIES = {
    'business_overview': 'business', 'products_services': 'products',
    'sales_orders': 'revenue_orders', 'risk_derivatives': 'financial_risk',
    'management_discussion': 'management', 'auditor_opinion': 'audit',
    'employees': 'employees',
}


class ReportDatabaseError(ValueError):
    """Safe, user-facing failure; upstream exception messages may contain keys."""


class ReportDataUnavailable(ReportDatabaseError):
    """A valid read found no usable report; this is not a connection failure."""


def safe_database_failure(exc: Exception, table: str) -> ReportDatabaseError:
    """Only display allowlisted error codes/types; never echo messages or URLs."""
    labels = {
        '42501': '조회 권한이 없습니다. 해당 테이블의 역할별 SELECT 권한을 확인해 주세요.',
        '42P01': '테이블을 찾지 못했습니다. 현재 프로젝트의 public 스키마에 있는지 확인해 주세요.',
        '42703': '조회한 열 이름이 DB 구조와 다릅니다. 테이블의 열 구성을 확인해 주세요.',
        'PGRST204': 'API에서 조회한 열을 찾지 못했습니다. 열 이름과 API 스키마 캐시를 확인해 주세요.',
        'PGRST205': 'API에서 테이블을 찾지 못했습니다. 회사 DB와 논문 DB가 같은 프로젝트인지, public 스키마에 있는지 확인해 주세요.',
        'PGRST106': '해당 스키마가 Data API에 노출되어 있지 않습니다.',
        'PGRST100': 'DB 조회 조건을 해석하지 못했습니다. 조회 코드와 열 형식을 확인해야 합니다.',
        'PGRST301': 'API 인증에 실패했습니다. 이 프로젝트의 서버용 키인지 확인해 주세요.',
        'PGRST302': '이 API 요청에 필요한 인증 정보가 없습니다.',
        'PGRST303': 'API 인증 정보 검증에 실패했습니다.',
        'PGRST003': 'DB 연결 대기 시간이 초과되었습니다. 잠시 후 다시 조회해 주세요.',
        '57014': 'DB 쿼리가 제한 시간 안에 끝나지 않았습니다.',
    }
    code = getattr(exc, 'code', None)
    if code is None and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get('code')
    if not isinstance(code, str) or code not in labels:
        code = None
    name = type(exc).__name__
    safe_types = {'ReadTimeout', 'ConnectTimeout', 'PoolTimeout', 'WriteTimeout',
                  'ConnectError', 'ReadError', 'RemoteProtocolError', 'TimeoutError'}
    table_name = table if table in {'companies', 'filings', 'report_sections', 'papers'} else '사업보고서'
    if code:
        return ReportDatabaseError(f'{table_name} 조회 실패 [{code}]: {labels[code]}')
    if name in safe_types:
        return ReportDatabaseError(f'{table_name} 조회 실패 [{name}]: 네트워크 연결 또는 응답 대기 시간을 확인해 주세요.')
    if isinstance(exc, TypeError):
        return ReportDatabaseError(f'{table_name} 조회 실패 [SDK_ARGUMENT]: 설치된 Supabase 패키지와 조회 코드의 호출 형식을 확인해야 합니다.')
    return ReportDatabaseError(f'{table_name} 조회 실패 [UNKNOWN]: 프로젝트·테이블 조회 권한·연결 상태를 확인해 주세요. '
                               'python check_report_db.py 로 단계별 점검을 실행할 수 있습니다.')


@dataclass(frozen=True)
class DatabaseSection(Section):
    corp_code: str
    rcept_no: str
    section_id: str
    source_locator: str
    financial_scope: str
    content_blocks: tuple
    source_kind: str = 'supabase_report'


@dataclass(frozen=True)
class DatabaseEvidence(Evidence):
    corp_code: str
    rcept_no: str
    section_id: str
    source_locator: str
    source_url: str
    financial_scope: str
    source_kind: str = 'supabase_report'


@dataclass(frozen=True)
class SavedReportEvidence(Evidence):
    source_kind: str = 'saved_report'


@dataclass
class ReportRetrieval:
    evidence: list[Evidence]
    notes: list[str]
    db_count: int
    file_count: int


def report_search_index(bundle, local_index, corp_name, fiscal_year) -> list[Evidence]:
    """DB first; only same-company/year local categories absent from the DB.

    Local JSON is a separate disclosed source. It is never relabeled as DB data.
    """
    database = [e for e in (bundle.index if bundle else [])
                if e.corp_name == corp_name and e.fiscal_year == fiscal_year]
    db_categories = {e.category for e in database}
    saved = [SavedReportEvidence(**e.to_dict()) for e in local_index
             if e.corp_name == corp_name and e.fiscal_year == fiscal_year
             and e.category not in db_categories]
    return database + saved


def retrieve_report(*, bundle, local_sections, local_index, corp_name, fiscal_year,
                    question, preset_id=None, db_error=None) -> ReportRetrieval:
    """Use DB excerpts, supplement missing categories, or search saved reports on failure."""
    index = report_search_index(bundle, local_index, corp_name, fiscal_year)
    database = [e for e in index if getattr(e, 'source_kind', '') == 'supabase_report']
    saved = [e for e in index if getattr(e, 'source_kind', '') == 'saved_report']
    requested = route_question(question, preset_id).categories
    missing = tuple(c for c in requested if c not in {e.category for e in database})
    has_supplement = any(e.category in missing for e in saved)
    db_evidence = retrieve(database, corp_name, fiscal_year, question, preset_id,
                           top_k=4 if has_supplement else 7)
    file_evidence = []
    if missing and has_supplement:
        file_evidence = retrieve(saved, corp_name, fiscal_year, question, preset_id,
                                 categories=missing, top_k=7 - len(db_evidence),
                                 max_chars=18000 - sum(len(e.text) for e in db_evidence))
    evidence = db_evidence + file_evidence
    notes = bundle.coverage(question, preset_id) if bundle else []
    if file_evidence:
        if bundle:
            notes.append('DB에서 제공하지 않는 항목을 같은 기업·사업연도의 저장 사업보고서 JSON에서 보충했습니다. '
                         '보충 문서는 별도 공시 버전일 수 있으므로 파일명·PDF 범위로 구분해 인용하고, '
                         'DB 수치와 충돌하면 합치지 말고 차이를 밝히세요.')
        else:
            notes.append('사업보고서 DB에서 사용할 본문을 확보하지 못해 같은 기업·같은 사업연도의 저장 사업보고서 JSON을 검색했습니다. '
                         '이 근거를 Supabase에서 읽었다고 표현하지 마세요.')
        notes.extend(coverage_notes(local_sections, corp_name, fiscal_year, question, preset_id))
        notes.append('saved_report는 저장 사업보고서에서 추출한 근거이며 실시간 DART 검색 결과가 아닙니다.')
    if db_error:
        notes.append('DB 조회 상태: ' + db_error)
    if not evidence:
        notes.append('DB와 같은 기업·같은 연도의 저장 사업보고서에서 질문에 맞는 공시 근거를 확보하지 못했습니다. '
                     '다른 기업·연도나 모델의 기억으로 기업 사실을 채우지 마세요.')
    return ReportRetrieval(evidence, list(dict.fromkeys(notes)), len(db_evidence), len(file_evidence))


@dataclass
class ReportBundle:
    corp_name: str
    corp_code: str
    fiscal_year: int
    rcept_no: str
    filing_date: str
    sections: list[DatabaseSection]
    index: list[DatabaseEvidence]
    warnings: list[str]
    revision: str

    def coverage(self, question: str, preset_id=None) -> list[str]:
        categories = {s.category for s in self.sections}
        requested = set(route_question(question, preset_id).categories)
        notes = [
            'DB 근거는 Supabase report_sections의 선택 기업·사업연도·활성 공시 본문입니다. '
            'PDF 페이지는 저장되어 있지 않으므로 접수번호·항목명·XML 위치로 출처를 표시합니다.',
            '재무 대시보드는 별도의 저장 분석 JSON입니다. 이번 답변에는 그 JSON이나 직원 현황 API 수치를 추가하지 않았습니다.',
            '본문의 당기·전기·전전기, 단위, 대상 법인과 연결·별도 기준을 함께 확인하세요. '
            'financial_scope가 비어 있으면 본문에 명시된 기준만 사용하세요.',
            *self.warnings,
        ]
        if requested & {'research_development', 'financial_statements', 'consolidated_notes', 'financial_notes'} - categories:
            notes.append('DB에는 사업·제품·매출·위험관리·경영진단·감사의견·직원 현황 중 검토 완료 항목만 연결했습니다. '
                         '전용 재무제표·주석·연구개발 자료의 미포함은 해당 활동이나 수치가 없다는 뜻이 아닙니다. '
                         '5년 수치 비교나 새로운 비율 계산은 제공하지 않습니다.')
        return notes


def uses_report_database(corp_name: str) -> bool:
    return corp_name in DB_COMPANIES


def report_document_name(item) -> str:
    return (f'Supabase · {item.corp_name} · {item.fiscal_year} 사업연도 · '
            f'{item.section} · 공시 접수번호 {item.rcept_no}')


def _clean(value) -> str:
    return '' if value is None or value == 'null' else str(value).strip()


def _table_parts(block: dict, context: str, size=4400):
    """Keep complete rows, merged-cell markers and the table's first rows together.

    First rows are repeated verbatim, not guessed to be semantic column headers.
    Oversized rows are omitted visibly, never cut into misleading numeric cells.
    """
    rows = block.get('rows')
    if not isinstance(rows, list) or not rows:
        return
    lines = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, list):
            continue
        cells = []
        for cell in row:
            if not isinstance(cell, dict):
                cells.append(str(cell))
                continue
            text = _clean(cell.get('text')).replace('\n', ' / ')
            spans = []
            for key in ('colspan', 'rowspan'):
                if str(cell.get(key, '1')) not in ('1', '', 'None'):
                    spans.append(f'{key}={cell[key]}')
            cells.append(text + (' {' + ', '.join(spans) + '}' if spans else ''))
        lines.append(f'{number}행: ' + ' | '.join(cells))
    lead = '[표 · 원문 행 순서; 병합된 셀의 범위를 함께 표시]\n' + context + '\n'
    first = '\n'.join(lines[:3])
    if len(first) > 2200:
        yield '[표 생략: 앞부분 행이 너무 깁니다. 공시 원문에서 머리글·단위를 확인하세요.]', '표 생략'
        return
    prefix = lead + '[표 앞 3행: 제목·단위·열 대응 확인용]\n' + first + '\n[이어지는 원문 행]\n'
    if len(lead) + sum(len(line) + 1 for line in lines) <= size:
        yield lead + '\n'.join(lines), f'표 1–{len(lines)}행'
        return
    current, first_number = [], 4
    for number, line in enumerate(lines[3:], 4):
        if len(prefix) + len(line) > size:
            line = f'{number}행: [긴 행 생략: 원문 확인 필요]'
        if current and len(prefix) + sum(len(x) + 1 for x in current) + len(line) > size:
            yield prefix + '\n'.join(current), f'표 앞 3행 + {first_number}–{number - 1}행'
            current, first_number = [], number
        current.append(line)
    if current:
        yield prefix + '\n'.join(current), f'표 앞 3행 + {first_number}–{len(lines)}행'
    elif not lines[3:]:
        yield lead + first, '표 앞 3행'


def build_database_index(sections: list[DatabaseSection]) -> list[DatabaseEvidence]:
    output = []
    for section in sections:
        bodies, paragraphs, context = [], [], []
        for number, block in enumerate(section.content_blocks, 1):
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'table':
                following = []
                for next_block in section.content_blocks[number:number + 3]:
                    if not isinstance(next_block, dict) or next_block.get('type') == 'table':
                        break
                    following.append(_clean(next_block.get('text')))
                table_context = ('[표 앞 문맥]\n' + '\n'.join(context[-4:])[-600:]
                                 + '\n[표 뒤 문맥]\n' + '\n'.join(following)[:600])
                for text, rows in _table_parts(block, table_context):
                    bodies.append((text, f'블록 {number} · {rows}'))
            else:
                text = _clean(block.get('text'))
                if text:
                    paragraphs.append(text)
                    context.append(text)
        # Invalid/absent block content falls back to the original DB text only.
        text = '\n\n'.join(paragraphs) if bodies or paragraphs else section.text
        if text:
            plain = Section(section.corp_name, section.fiscal_year, section.section, section.category,
                            section.source_file, 0, 0, text)
            bodies = [(chunk.text, f'본문 발췌 {n}') for n, chunk in enumerate(split_section(plain), 1)] + bodies
        position = 0
        for text, location in bodies:
            seed = f'{section.corp_code}|{section.fiscal_year}|{section.rcept_no}|{section.section_id}|{location}|{text}'
            output.append(DatabaseEvidence(
                source_id='S' + hashlib.sha256(seed.encode()).hexdigest()[:10],
                corp_name=section.corp_name, fiscal_year=section.fiscal_year,
                section=section.section, category=section.category, source_file=section.source_file,
                start_pdf_page=0, end_pdf_page=0, text=text, char_start=position, char_end=position + len(text),
                corp_code=section.corp_code, rcept_no=section.rcept_no, section_id=section.section_id,
                source_locator=section.source_locator + ' · ' + location,
                source_url=f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={section.rcept_no}',
                financial_scope=section.financial_scope,
            ))
            position += len(text) + 1
    return output


class ReportRepository:
    def __init__(self, url: str, secret_key: str, *, client=None):
        try:
            self._connection = PaperRepository(url, secret_key, client=client)
        except Exception:
            raise ReportDatabaseError('회사 DB의 URL·서버용 키 설정을 확인해 주세요. 별도 회사 DB는 '
                                      'REPORT_SUPABASE_URL / REPORT_SUPABASE_SECRET_KEY를 사용합니다.') from None

    def _rows(self, table, columns, filters, order):
        output = []
        try:
            while True:
                query = self._connection._db().table(table).select(columns)
                for key, value in filters.items():
                    query = query.eq(key, value)
                batch = query.order(order).range(len(output), len(output) + 199).execute().data
                if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                    raise ReportDatabaseError('사업보고서 DB의 응답 형식이 올바르지 않습니다.')
                output.extend(batch)
                if len(batch) < 200:
                    return output
                if len(output) >= 2000:
                    raise ReportDatabaseError('사업보고서 조회 범위가 너무 큽니다. 기업·연도·활성 공시 설정을 확인해 주세요.')
        except ReportDatabaseError:
            raise
        except Exception as exc:
            raise safe_database_failure(exc, table) from None

    def diagnose(self, tables=None) -> list[tuple[str, bool, str]]:
        """Probe individual table access with a single row, never dumping records."""
        results = []
        columns = {'papers': 'paper_id', 'companies': 'corp_code',
                   'filings': 'rcept_no', 'report_sections': 'section_id'}
        for table in (tables if tables is not None else columns):
            column = columns[table]
            try:
                rows = (self._connection._db().table(table).select(column)
                        .order(column).range(0, 0).execute()).data
                if not isinstance(rows, list):
                    results.append((table, False, '응답 형식을 확인할 수 없습니다.'))
                elif rows:
                    results.append((table, True, '조회 성공 · 행 접근 확인'))
                else:
                    results.append((table, True, '조회 성공 · 현재 키로 보이는 행 0개: 빈 테이블 또는 RLS 정책을 확인해 주세요.'))
            except Exception as exc:
                results.append((table, False, str(safe_database_failure(exc, table))))
        return results

    def years(self, corp_name: str) -> list[int]:
        code = DB_COMPANIES.get(corp_name)
        if not code:
            raise ReportDatabaseError('이 기업은 아직 사업보고서 DB 연결 대상이 아닙니다.')
        rows = self._rows('filings', 'corp_code,fiscal_year,report_type,is_active,parse_status',
                          {'corp_code': code, 'report_type': 'annual', 'is_active': True}, 'fiscal_year')
        return sorted({int(r['fiscal_year']) for r in rows if r.get('corp_code') == code
                       and r.get('parse_status') == 'parsed' and 1900 <= int(r['fiscal_year']) <= 2200}, reverse=True)

    def load(self, corp_name: str, fiscal_year: int) -> ReportBundle:
        code = DB_COMPANIES.get(corp_name)
        if not code or not 1900 <= fiscal_year <= 2200:
            raise ReportDatabaseError('선택한 기업·사업연도를 확인해 주세요.')
        companies = self._rows('companies', 'corp_code,corp_name', {'corp_code': code}, 'corp_code')
        if not companies:
            raise ReportDataUnavailable(f'회사 DB의 companies에 {corp_name}({code})이 저장되어 있지 않습니다.')
        if len(companies) != 1 or companies[0].get('corp_code') != code or companies[0].get('corp_name') != corp_name:
            raise ReportDatabaseError(f'companies의 기업명·고유번호가 {corp_name}({code})과 일치하지 않습니다.')
        filings = self._rows('filings', 'rcept_no,corp_code,fiscal_year,report_type,is_active,parse_status,filing_date',
                             {'corp_code': code, 'fiscal_year': fiscal_year, 'report_type': 'annual', 'is_active': True}, 'rcept_no')
        if not filings:
            raise ReportDataUnavailable(f'회사 DB에 {corp_name} · {fiscal_year}년의 활성 사업보고서가 없습니다.')
        if len(filings) != 1:
            raise ReportDatabaseError('선택 사업연도의 활성 사업보고서는 정확히 1개여야 합니다. filings의 is_active와 fiscal_year를 확인해 주세요.')
        filing = filings[0]
        receipt = _clean(filing.get('rcept_no'))
        if (filing.get('corp_code') != code or int(filing.get('fiscal_year', 0)) != fiscal_year
                or filing.get('report_type') != 'annual' or filing.get('is_active') is not True
                or not re.fullmatch(r'\d{14}', receipt)):
            raise ReportDatabaseError('선택 공시의 기업·연도·접수번호·활성 상태가 조회 조건과 일치하지 않습니다.')
        if filing.get('parse_status') != 'parsed':
            raise ReportDataUnavailable(f'{corp_name} · {fiscal_year}년의 공시는 DB에 있지만 '
                                        '본문 검토가 완료(parsed)되지 않았습니다.')
        rows = self._rows('report_sections', '*', {'rcept_no': receipt}, 'section_id')
        sections, excluded, seen = [], 0, set()
        for row in rows:
            category = SECTION_CATEGORIES.get(row.get('section_key'))
            if row.get('rcept_no') != receipt:
                raise ReportDatabaseError('DB 응답에 선택 공시와 다른 본문이 포함되어 있습니다.')
            if (row.get('extraction_status') != 'success' or row.get('source_type') != 'core'
                    or not category or not _clean(row.get('text_content'))):
                excluded += 1
                continue
            section_id = _clean(row.get('section_id'))
            if not section_id or section_id in seen:
                raise ReportDatabaseError('사업보고서 본문의 section_id가 비어 있거나 중복되어 있습니다.')
            seen.add(section_id)
            blocks = row.get('content_blocks') or []
            if isinstance(blocks, str):
                try:
                    blocks = json.loads(blocks)
                except ValueError:
                    blocks = []
            if not isinstance(blocks, list):
                blocks = []
            sections.append(DatabaseSection(
                corp_name=corp_name, fiscal_year=fiscal_year, section=_clean(row.get('section_title')),
                category=category, source_file=f'Supabase · {corp_name} {fiscal_year} 사업보고서 · {receipt}',
                start_pdf_page=0, end_pdf_page=0, text=_clean(row.get('text_content')),
                corp_code=code, rcept_no=receipt, section_id=section_id,
                source_locator=_clean(row.get('source_locator')) or _clean(row.get('heading_path')),
                financial_scope=_clean(row.get('financial_scope')), content_blocks=tuple(blocks),
            ))
        if not sections:
            raise ReportDataUnavailable(f'{corp_name} · {fiscal_year}년의 공시에 검토 완료(success) 본문이 없습니다. '
                                        'report_sections의 상태와 데이터를 확인해 주세요.')
        warnings = [f'미검토·미지원·빈 본문 {excluded}개 항목은 답변에서 제외했습니다.'] if excluded else []
        revision = hashlib.sha256(json.dumps([asdict(s) for s in sections], ensure_ascii=False,
                                            sort_keys=True).encode()).hexdigest()
        return ReportBundle(corp_name, code, fiscal_year, receipt, _clean(filing.get('filing_date')),
                            sections, build_database_index(sections), warnings, revision)
