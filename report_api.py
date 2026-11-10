"""OpenDART report source. No database access or saved-report fallback.

List final annual filings, download that filing's original ZIP, parse its main
XML, and retrieve only excerpts from the selected corporation and fiscal year.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zipfile import BadZipFile, ZipFile

import requests
from bs4 import BeautifulSoup, Tag

from core import Evidence, Section, retrieve, route_question, split_section

API_BASE = 'https://opendart.fss.or.kr/api'
API_COMPANIES = {
    '이삭엔지니어링': '01135640', '롯데이노베이트': '01262032',
    '인성정보': '00229021', '링네트': '00352499', '에스트래픽': '01014055',
}
MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_XML_BYTES = 80 * 1024 * 1024
STATUS_MESSAGES = {
    '010': '등록되지 않은 인증키입니다. DART_API_KEY를 확인해 주세요.',
    '011': '사용할 수 없는 인증키입니다.', '012': '허용되지 않은 접속 IP입니다.',
    '013': '선택 조건에 맞는 공시가 없습니다.', '014': '공시 원문 파일이 없습니다.',
    '020': 'OpenDART 요청 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.',
    '100': 'OpenDART 요청 조건을 확인해 주세요.',
    '800': 'OpenDART 시스템 점검 중입니다. 잠시 후 다시 시도해 주세요.',
    '900': 'OpenDART에서 오류가 발생했습니다.', '901': '인증키 사용 기간이 만료되었습니다.',
}


class ReportAPIError(ValueError):
    """Only safe, allowlisted text reaches the UI; never include request URLs."""


class ReportAPINoData(ReportAPIError):
    pass


@dataclass(frozen=True)
class APISection(Section):
    corp_code: str
    rcept_no: str
    section_id: str
    source_locator: str
    financial_scope: str
    content_blocks: tuple
    source_kind: str = 'opendart_report'


@dataclass(frozen=True)
class APIEvidence(Evidence):
    corp_code: str
    rcept_no: str
    section_id: str
    source_locator: str
    source_url: str
    financial_scope: str
    source_kind: str = 'opendart_report'


@dataclass
class APIReportBundle:
    corp_name: str
    corp_code: str
    fiscal_year: int
    rcept_no: str
    filing_date: str
    report_name: str
    fetched_at: str
    sections: list[APISection]
    index: list[APIEvidence]
    warnings: list[str]
    revision: str

    def coverage(self, question, preset_id=None):
        requested = set(route_question(question, preset_id).categories)
        available = {s.category for s in self.sections}
        notes = [
            '공시 근거는 OpenDART API에서 내려받은 선택 기업·사업연도·접수번호의 사업보고서 원문입니다.',
            'PDF 페이지는 제공하지 않습니다. 접수번호·원문 파일·제목 경로와 본문 위치로 출처를 확인하세요.',
            '재무 차트는 별도의 저장 분석 자료입니다. 그 수치와 직원 현황 저장본은 이번 LLM 근거에 자동으로 섞지 않습니다.',
            '당기·전기·전전기, 단위, 대상 법인, 연결·별도 기준을 원문에서 함께 확인하세요. 비율을 새로 계산하지 마세요.',
            *self.warnings,
        ]
        missing = requested - available
        if missing:
            from core import CATEGORY_LABELS
            notes.append('이번 API 원문 추출에서 확보하지 못한 항목: ' +
                         ', '.join(CATEGORY_LABELS.get(c, c) for c in sorted(missing)) +
                         '. 해당 사업이나 제도가 없다는 뜻은 아닙니다. 다른 기업·연도나 모델 기억으로 보충하지 마세요.')
        return notes


def report_document_name(item):
    return (f'OpenDART API · {item.corp_name} · {item.fiscal_year} 사업연도 · '
            f'{item.section} · 공시 접수번호 {item.rcept_no}')


def _normal_name(value):
    """Normalize legal-form notation without removing letters from a name.

    DART's display name often omits the (주) found in COMPANY-NAME. Stripping
    parentheses first leaves a stray 주, incorrectly rejecting that document.
    NFKC also turns ㈜ and full-width parentheses into the same (주) marker.
    Only legal-form markers at the edges are removed, never a bare 주.
    """
    text = unicodedata.normalize('NFKC', str(value))
    text = re.sub(r'[\s\u200b\u200c\u200d\u2060\ufeff]', '', text)
    legal_form = r'(?:주식회사|\(주\))'
    return re.sub(rf'^(?:{legal_form})+|(?:{legal_form})+$', '', text)


def _status_error(status):
    status = str(status)
    label = STATUS_MESSAGES.get(status, 'OpenDART 응답 상태를 확인할 수 없습니다.')
    code = status if status in STATUS_MESSAGES else 'UNKNOWN'
    cls = ReportAPINoData if status in {'013', '014'} else ReportAPIError
    return cls(f'공시 API [{code}]: {label}')


def _year(row):
    match = re.search(r'사업보고서\s*\(\s*(\d{4})\s*\.\s*(\d{1,2})\s*\)',
                      str(row.get('report_nm', '')))
    return int(match[1]) if match and 1 <= int(match[2]) <= 12 else None


class ReportAPIClient:
    def __init__(self, api_key, *, session=None, today=None):
        self._key = str(api_key).strip()
        if not self._key or self._key.startswith('YOUR_'):
            raise ReportAPIError('공시 API 연결에 DART_API_KEY가 필요합니다. 공시 API 연결 설정에 키를 입력하세요.')
        self._session = session or requests
        self._today = today or date.today()

    def _get(self, endpoint, params, *, binary=False):
        try:
            response = self._session.get(API_BASE + '/' + endpoint,
                params={**params, 'crtfc_key': self._key}, timeout=(5, 45))
            response.raise_for_status()
        except requests.Timeout:
            raise ReportAPIError('공시 API 응답 시간이 초과되었습니다. 새로 불러오기로 다시 시도해 주세요.') from None
        except requests.RequestException:
            raise ReportAPIError('공시 API에 연결하지 못했습니다. 네트워크·접속 제한을 확인해 주세요.') from None
        if binary:
            body = response.content
            if len(body) > MAX_ZIP_BYTES:
                raise ReportAPIError('공시 원문 파일이 처리 가능한 크기를 초과했습니다.')
            if body[:2] != b'PK':
                soup = BeautifulSoup(body, 'html.parser')
                status = soup.find('status')
                raise _status_error(status.get_text(strip=True) if status else 'UNKNOWN')
            return body
        try:
            payload = response.json()
        except ValueError:
            raise ReportAPIError('공시 API의 JSON 응답을 읽지 못했습니다.') from None
        if not isinstance(payload, dict):
            raise ReportAPIError('공시 API의 응답 형식이 올바르지 않습니다.')
        status = str(payload.get('status'))
        if status == '013':
            return {'list': [], 'total_page': 1}
        if status != '000':
            raise _status_error(status)
        return payload

    def filings(self, corp_name):
        if corp_name not in API_COMPANIES:
            raise ReportAPIError('지원하는 기업을 선택해 주세요.')
        code = API_COMPANIES[corp_name]
        output, page, total = [], 1, 1
        while page <= total:
            payload = self._get('list.json', {
                'corp_code': code, 'bgn_de': f'{self._today.year - 7}0101',
                'end_de': self._today.strftime('%Y%m%d'), 'last_reprt_at': 'Y',
                'pblntf_detail_ty': 'A001', 'sort': 'date', 'sort_mth': 'desc',
                'page_count': '100', 'page_no': str(page),
            })
            rows = payload.get('list', [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ReportAPIError('공시 목록의 응답 형식이 올바르지 않습니다.')
            for row in rows:
                if str(row.get('corp_code', '')) != code:
                    raise ReportAPIError('공시 API의 기업 고유번호가 선택 기업과 다릅니다.')
                if '철' in str(row.get('rm', '')):
                    continue
                # Annual reports only. No prior-year substitution on a missing year.
                if _year(row) is None:
                    continue
                if not re.fullmatch(r'\d{14}', str(row.get('rcept_no', ''))):
                    raise ReportAPIError('공시 접수번호 형식이 올바르지 않습니다.')
                output.append(row)
            try:
                total = int(payload.get('total_page', 1))
            except (TypeError, ValueError):
                raise ReportAPIError('공시 목록의 페이지 정보를 읽지 못했습니다.') from None
            if not 0 <= total <= 20:
                raise ReportAPIError('공시 목록 조회 범위가 너무 큽니다.')
            page += 1
        return sorted(output, key=lambda r: (str(r.get('rcept_dt', '')), r['rcept_no']), reverse=True)

    def load(self, corp_name, fiscal_year, *, filings=None):
        if corp_name not in API_COMPANIES:
            raise ReportAPIError('지원하는 기업을 선택해 주세요.')
        rows = self.filings(corp_name) if filings is None else filings
        code = API_COMPANIES[corp_name]
        rows = [r for r in rows if str(r.get('corp_code')) == code and _year(r) == int(fiscal_year)
                and '철' not in str(r.get('rm', ''))]
        if not rows:
            raise ReportAPINoData(f'{corp_name}의 {fiscal_year} 사업연도 최종 사업보고서가 API에서 조회되지 않았습니다.')
        # last_reprt_at=Y already selects final versions. The latest received
        # filing wins if more than one final entry is returned for the same period.
        filing = max(rows, key=lambda r: (str(r.get('rcept_dt', '')), r['rcept_no']))
        receipt = str(filing.get('rcept_no', ''))
        if not re.fullmatch(r'\d{14}', receipt):
            raise ReportAPIError('공시 접수번호 형식이 올바르지 않습니다.')
        archive = self._get('document.xml', {'rcept_no': receipt}, binary=True)
        sections, warnings = parse_report_archive(archive, corp_name, int(fiscal_year), receipt)
        if not sections:
            raise ReportAPINoData('공시 원문은 받았지만 지원 항목의 본문을 추출하지 못했습니다. DART 원문을 확인해 주세요.')
        index = build_api_index(sections)
        if not index:
            raise ReportAPINoData('선택 공시에서 답변에 사용할 본문을 확보하지 못했습니다.')
        revision = hashlib.sha256(archive).hexdigest()
        return APIReportBundle(corp_name, code, int(fiscal_year), receipt,
            str(filing.get('rcept_dt', '')), str(filing.get('report_nm', '')),
            datetime.now(timezone.utc).isoformat(), sections, index, warnings, revision)


def _category(title):
    text = re.sub(r'\s+', '', title)
    for pattern, category in (
        ('연결재무제표주석', 'consolidated_notes'),
        ('재무제표주석|별도재무제표주석', 'financial_notes'),
        ('연구개발|연구및개발', 'research_development'),
        ('임원및직원|직원등의현황|직원의현황|직원현황', 'employees'),
        ('감사인의|감사의견|외부감사', 'audit'),
        ('경영진단|경영분석', 'management'),
        ('위험관리|파생거래|파생상품|주요계약', 'financial_risk'),
        ('매출|수주', 'revenue_orders'),
        ('주요제품|제품및서비스|제품과서비스|원재료|생산설비', 'products'),
        ('재무제표', 'financial_statements'),
        ('사업의개요|사업개요|사업의내용', 'business'),
    ):
        if re.search(pattern, text):
            return category
    return None


def _decode_xml(raw):
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise ReportAPIError('공시 원문의 문자 인코딩을 읽지 못했습니다.')


def parse_report_archive(archive, corp_name, fiscal_year, receipt):
    candidates = []
    try:
        with ZipFile(io.BytesIO(archive)) as zipped:
            entries = [e for e in zipped.infolist() if e.filename.lower().endswith(('.xml', '.html', '.htm'))]
            if len(entries) > 100 or sum(e.file_size for e in entries) > MAX_XML_BYTES:
                raise ReportAPIError('공시 원문의 압축 해제 크기가 처리 한도를 초과했습니다.')
            for entry in entries:
                soup = BeautifulSoup(_decode_xml(zipped.read(entry)), 'html.parser')
                name = soup.find('document-name')
                if name and (str(name.get('acode', '')) == '11011' or '사업보고서' in name.get_text()):
                    candidates.append((entry.filename, soup))
    except (BadZipFile, RuntimeError, OSError, EOFError):
        raise ReportAPIError('공시 원문 ZIP 파일을 읽지 못했습니다.') from None
    if len(candidates) != 1:
        raise ReportAPIError('ZIP에서 사업보고서 본문을 하나로 식별하지 못했습니다. 첨부 문서를 임의로 사용하지 않습니다.')
    filename, soup = candidates[0]
    identity = soup.find('company-name')
    if identity is None:
        raise ReportAPIError('공시 원문에서 회사명 태그를 찾지 못했습니다.')
    if _normal_name(identity.get_text()) != _normal_name(corp_name):
        original_name = ' '.join(identity.get_text(' ', strip=True).split())[:80]
        raise ReportAPIError(f'공시 원문의 회사명이 선택 기업과 일치하지 않습니다. '
                             f'(선택: {corp_name} · 원문: {original_name})')
    identity_code = identity.get('aregcik')
    if identity_code and str(identity_code).zfill(8) != API_COMPANIES[corp_name]:
        raise ReportAPIError('공시 원문의 기업 고유번호가 선택 기업과 다릅니다.')
    periods = soup.find_all('period')
    end_dates = [p.get_text(' ', strip=True) for p in periods
                 if any(t in str(p.get('aunit', '')).lower() for t in ('end', 'periodto'))]
    if end_dates and not any(re.search(rf'\b{fiscal_year}\b', p) for p in end_dates):
        raise ReportAPIError('공시 원문 결산연도가 선택 사업연도와 다릅니다.')
    groups, warnings = [], []
    headings = {'title', 'subtitle', 'h1', 'h2', 'h3', 'h4'}
    paragraphs = {'p', 'tu', 'te', 'div', 'li'}

    def walk(container, path=(), category=None):
        current_path, current_category, current = path, category, None
        for node in container.children:
            if not isinstance(node, Tag):
                continue
            tag = node.name.lower()
            if tag in headings:
                title = node.get_text(' ', strip=True)
                current_path = (*path, title)
                current_category = _category(title) or category
                current = None
            elif tag in {'script', 'style'}:
                continue
            elif tag == 'table':
                if current_category:
                    if current is None:
                        current = {'path': current_path, 'category': current_category, 'blocks': []}
                        groups.append(current)
                    rows = []
                    for row in node.find_all('tr'):
                        if row.find_parent('table') is not node:
                            continue
                        cells = []
                        for cell in row.find_all(['td', 'th', 'te', 'tu'], recursive=False):
                            cells.append({'text': cell.get_text(' ', strip=True),
                                          'colspan': cell.get('colspan', '1'), 'rowspan': cell.get('rowspan', '1')})
                        if cells:
                            rows.append(cells)
                    if rows:
                        current['blocks'].append({'type': 'table', 'rows': rows})
            elif tag in paragraphs and not node.find(['table', *headings, 'p']):
                text = node.get_text(' ', strip=True)
                if text and current_category:
                    if current is None:
                        current = {'path': current_path, 'category': current_category, 'blocks': []}
                        groups.append(current)
                    current['blocks'].append({'type': 'paragraph', 'text': text})
            else:
                walk(node, current_path, current_category)
                current = None

    walk(soup)
    sections = []
    for i, group in enumerate(groups, 1):
        blocks = group['blocks']
        if not blocks:
            continue
        title = ' / '.join(group['path']) or group['category']
        text = '\n\n'.join(b.get('text', '') if b['type'] != 'table' else
                           '\n'.join(' | '.join(c['text'] for c in row) for row in b['rows']) for b in blocks)
        sections.append(APISection(corp_name, fiscal_year, title, group['category'],
            f'OpenDART_{corp_name}_{fiscal_year}_{receipt}_{filename}', 0, 0, text,
            API_COMPANIES[corp_name], receipt, f'api-section-{i}',
            f'{filename} · {title} · 추출 블록 {i}', '', tuple(blocks)))
    if not any(s.category == 'employees' for s in sections):
        warnings.append('직원 현황 전용 항목을 추출하지 못했습니다. 급여·근속·인원은 근거가 확인될 때만 설명하세요.')
    warnings.append('본문 제목과 표 구조를 자동 추출한 자료입니다. 병합 셀·단위·열의 대응이 불명확하면 수치를 단정하지 마세요.')
    return sections, warnings


def retrieve_api_report(bundle, corp_name, fiscal_year, question, preset_id=None):
    if bundle is None:
        raise ReportAPINoData('공시 API에서 원문을 불러온 뒤 질문해 주세요.')
    if bundle.corp_name != corp_name or bundle.fiscal_year != fiscal_year:
        raise ReportAPIError('선택 기업·사업연도와 API 보고서가 일치하지 않습니다.')
    evidence = retrieve(bundle.index, corp_name, fiscal_year, question, preset_id, top_k=7)
    notes = bundle.coverage(question, preset_id)
    if not evidence:
        notes.append('API 원문에서 질문에 필요한 근거를 찾지 못했습니다. 저장 파일이나 모델 기억으로 보충하지 마세요.')
    return evidence, notes


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


def build_api_index(sections: list[APISection]) -> list[APIEvidence]:
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
        # Invalid/absent block content falls back to the original API text only.
        text = '\n\n'.join(paragraphs) if bodies or paragraphs else section.text
        if text:
            plain = Section(section.corp_name, section.fiscal_year, section.section, section.category,
                            section.source_file, 0, 0, text)
            bodies = [(chunk.text, f'본문 발췌 {n}') for n, chunk in enumerate(split_section(plain), 1)] + bodies
        position = 0
        for text, location in bodies:
            seed = f'{section.corp_code}|{section.fiscal_year}|{section.rcept_no}|{section.section_id}|{location}|{text}'
            output.append(APIEvidence(
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
