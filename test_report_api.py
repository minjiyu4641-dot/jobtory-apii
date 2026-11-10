import io
import json
from datetime import date
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import requests

from answer_service import generate_answer
from report_api import (API_COMPANIES, APIEvidence, ReportAPIClient, ReportAPIError,
                        ReportAPINoData, _year, parse_report_archive, retrieve_api_report)


KEY = 'test-key-never-display'
RECEIPT = '20260323000001'


def xml_report(company='이삭엔지니어링', year=2025, *, rows=4):
    return f'''<DOCUMENT><DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME>
    <COMPANY-NAME AREGCIK="{API_COMPANIES[company]}">{company} 주식회사</COMPANY-NAME>
    <PERIOD AUNIT="PERIODTO">{year}.12.31</PERIOD><BODY>
    <SECTION-1><TITLE>II. 사업의 내용</TITLE>
    <SECTION-2><TITLE>1. 사업의 개요</TITLE>
    <P>당사는 스마트팩토리와 공장 자동화 솔루션을 제공하고 있습니다. 고객에게 시스템을 공급합니다.</P></SECTION-2>
    <SECTION-2><TITLE>4. 매출 및 수주상황</TITLE><P>(단위: 백만원)</P>
    <TABLE><TR><TH ROWSPAN="2">사업부문</TH><TH COLSPAN="2">매출액</TH></TR>
    <TR><TH>{year}년</TH><TH>{year-1}년</TH></TR>
    {''.join(f'<TR><TD>자동화사업 {n}</TD><TD>100</TD><TD>80</TD></TR>' for n in range(rows))}</TABLE>
    <P>연결재무제표 기준이며 단위는 백만원입니다.</P></SECTION-2>
    <SECTION-2><TITLE>6. 주요계약 및 연구개발활동</TITLE><P>스마트팩토리 연구개발활동을 수행합니다.</P></SECTION-2>
    </SECTION-1><SECTION-1><TITLE>VIII. 임원 및 직원 등에 관한 사항</TITLE>
    <SECTION-2><TITLE>1. 임원 및 직원 등의 현황</TITLE><P>직원 현황의 평균급여는 신입 초봉이 아닙니다.</P>
    </SECTION-2></SECTION-1></BODY></DOCUMENT>'''


def zipped(xml, extra=None, encoding='utf-8'):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as z:
        z.writestr(RECEIPT + '.xml', xml.encode(encoding))
        if extra:
            z.writestr('attachment.xml', extra.encode(encoding))
    return stream.getvalue()


def filing(company='이삭엔지니어링', year=2025, receipt=RECEIPT, **kwargs):
    return {'corp_code': API_COMPANIES[company], 'corp_name': company,
            'report_nm': f'사업보고서 ({year}.12)', 'rcept_no': receipt,
            'rcept_dt': receipt[:8], 'rm': '', **kwargs}


class Response:
    def __init__(self, payload=None, content=b''):
        self.payload, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, company='이삭엔지니어링', *, rows=None, archive=None):
        self.company, self.calls = company, []
        self.rows = [filing(company)] if rows is None else rows
        self.archive = zipped(xml_report(company)) if archive is None else archive

    def get(self, url, *, params, timeout):
        self.calls.append((url, params))
        if url.endswith('list.json'):
            return Response({'status': '000', 'total_page': 1, 'list': self.rows})
        return Response(content=self.archive)


@pytest.mark.parametrize('company', API_COMPANIES)
def test_five_companies_use_their_own_api_identity(company):
    session = FakeSession(company)
    bundle = ReportAPIClient(KEY, session=session, today=date(2026, 11, 10)).load(company, 2025)
    assert session.calls[0][1]['corp_code'] == API_COMPANIES[company]
    assert session.calls[0][1]['last_reprt_at'] == 'Y'
    assert session.calls[0][1]['pblntf_detail_ty'] == 'A001'
    assert session.calls[1][1]['rcept_no'] == RECEIPT
    assert bundle.corp_name == company
    assert all(isinstance(e, APIEvidence) and e.source_kind == 'opendart_report' for e in bundle.index)
    assert all(e.corp_code == API_COMPANIES[company] and e.rcept_no == RECEIPT for e in bundle.index)
    assert KEY not in repr(bundle)


def test_latest_corrected_filing_is_downloaded():
    newer = '20260903000001'
    rows = [filing(receipt=RECEIPT), filing(receipt=newer, report_nm='[기재정정]사업보고서 (2025.12)'),
            filing(year=2024, receipt='20250303000001')]
    session = FakeSession(rows=rows)
    bundle = ReportAPIClient(KEY, session=session).load('이삭엔지니어링', 2025)
    assert bundle.rcept_no == newer
    assert session.calls[-1][1]['rcept_no'] == newer


def test_missing_year_does_not_download_previous_report():
    session = FakeSession(rows=[filing(year=2024)])
    with pytest.raises(ReportAPINoData):
        ReportAPIClient(KEY, session=session).load('이삭엔지니어링', 2025)
    assert len(session.calls) == 1


def test_mismatched_company_is_rejected():
    session = FakeSession(rows=[filing('링네트')])
    with pytest.raises(ReportAPIError, match='고유번호'):
        ReportAPIClient(KEY, session=session).load('이삭엔지니어링', 2025)


@pytest.mark.parametrize('company', API_COMPANIES)
@pytest.mark.parametrize('template', ['(주){}', '{}(주)', '㈜ {}', '{} ㈜',
                                     '（주）{}', '{}（주）', '주식회사 {}', '{} 주식회사'])
def test_legal_company_notation_does_not_block_a_matching_report(company, template):
    original = xml_report(company)
    source = original.replace(f'>{company} 주식회사</COMPANY-NAME>',
                              f'>{template.format(company)}</COMPANY-NAME>')
    session = FakeSession(company, archive=zipped(source))
    bundle = ReportAPIClient(KEY, session=session).load(company, 2025)
    assert bundle.corp_name == company
    assert bundle.corp_code == API_COMPANIES[company]
    assert bundle.index and all(e.corp_name == company for e in bundle.index)


@pytest.mark.parametrize('name', ['주에스트래픽', '에스트래픽주', '에스트래픽솔루션', '(주)에스트래픽서비스'])
def test_similar_names_cannot_pass_identity_check(name):
    source = xml_report('에스트래픽').replace('>에스트래픽 주식회사</COMPANY-NAME>',
                                            f'>{name}</COMPANY-NAME>')
    with pytest.raises(ReportAPIError, match='회사명'):
        parse_report_archive(zipped(source), '에스트래픽', 2025, RECEIPT)


def test_legal_notation_does_not_bypass_corporation_code_check():
    source = xml_report('에스트래픽').replace('>에스트래픽 주식회사</COMPANY-NAME>',
                                            '>(주)에스트래픽</COMPANY-NAME>')
    source = source.replace('AREGCIK="01014055"', 'AREGCIK="01135640"')
    with pytest.raises(ReportAPIError, match='고유번호'):
        parse_report_archive(zipped(source), '에스트래픽', 2025, RECEIPT)


@pytest.mark.parametrize('status', ['010', '012', '020', '800', '901'])
def test_api_failures_never_echo_server_message_or_key(status):
    session = SimpleNamespace(get=lambda *a, **k: Response({'status': status, 'message': KEY}))
    with pytest.raises(ReportAPIError) as caught:
        ReportAPIClient(KEY, session=session).filings('이삭엔지니어링')
    assert KEY not in str(caught.value)
    assert status in str(caught.value)


def test_network_failure_does_not_leak_request_url():
    def fail(*a, **k):
        raise requests.ConnectionError('https://example.test?crtfc_key=' + KEY)
    with pytest.raises(ReportAPIError) as caught:
        ReportAPIClient(KEY, session=SimpleNamespace(get=fail)).filings('이삭엔지니어링')
    assert KEY not in str(caught.value)
    assert 'https://' not in str(caught.value)


def test_document_error_xml_is_not_treated_as_zip():
    session = FakeSession(archive=f'<result><status>020</status><message>{KEY}</message></result>'.encode())
    with pytest.raises(ReportAPIError, match='020') as caught:
        ReportAPIClient(KEY, session=session).load('이삭엔지니어링', 2025)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize('xml', [xml_report('링네트'), xml_report(year=2024)])
def test_wrong_xml_company_or_period_is_rejected(xml):
    with pytest.raises(ReportAPIError):
        parse_report_archive(zipped(xml), '이삭엔지니어링', 2025, RECEIPT)


def test_attachment_does_not_replace_or_blend_main_document():
    archive = zipped(xml_report(), '<DOCUMENT><DOCUMENT-NAME>첨부문서</DOCUMENT-NAME><P>첨부 전용 문장</P></DOCUMENT>')
    session = FakeSession(archive=archive)
    bundle = ReportAPIClient(KEY, session=session).load('이삭엔지니어링', 2025)
    assert all('첨부 전용 문장' not in s.text for s in bundle.sections)
    with pytest.raises(ReportAPIError, match='하나로 식별'):
        parse_report_archive(zipped(xml_report(), xml_report()), '이삭엔지니어링', 2025, RECEIPT)


def test_large_table_chunks_keep_headers_units_and_whole_rows():
    session = FakeSession(archive=zipped(xml_report(rows=250)))
    bundle = ReportAPIClient(KEY, session=session).load('이삭엔지니어링', 2025)
    tables = [e for e in bundle.index if e.text.startswith('[표 ·')]
    assert len(tables) > 1
    for e in tables:
        assert '백만원' in e.text and '2025년' in e.text and '2024년' in e.text
        assert 'rowspan=2' in e.text and 'colspan=2' in e.text
    assert all(f'자동화사업 {n} | 100 | 80' in '\n'.join(e.text for e in tables) for n in range(250))


def test_cp949_originals_are_decoded():
    sections, _ = parse_report_archive(zipped(xml_report(), encoding='cp949'), '이삭엔지니어링', 2025, RECEIPT)
    assert any('스마트팩토리' in s.text for s in sections)


def test_api_evidence_and_citations_reach_existing_llm_call():
    bundle = ReportAPIClient(KEY, session=FakeSession()).load('이삭엔지니어링', 2025)
    evidence, notes = retrieve_api_report(bundle, '이삭엔지니어링', 2025, '주요 사업을 설명해줘.')
    captured = {}
    def parse(**kwargs):
        captured.update(kwargs)
        claim = {'text': '회사는 공장 자동화 솔루션을 제공한다고 설명합니다.', 'source_ids': [evidence[0].source_id]}
        return SimpleNamespace(status='completed', output_parsed={
            'status': 'answered', 'summary': claim, 'facts': [claim], 'interpretations': [],
            'follow_up_questions': [], 'limitations': []})
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = generate_answer(question='주요 사업을 설명해줘.', corp_name='이삭엔지니어링', fiscal_year=2025,
                             evidence=evidence, notes=notes, api_key='', client=client)
    payload = json.loads(captured['input'])
    assert payload['evidence']
    assert all(e['source_kind'] == 'opendart_report' and e['rcept_no'] == RECEIPT for e in payload['evidence'])
    assert result.answer.status == 'answered'
    assert captured['store'] is False


def test_retrieval_rejects_wrong_selection_and_has_no_fallback():
    bundle = ReportAPIClient(KEY, session=FakeSession()).load('이삭엔지니어링', 2025)
    with pytest.raises(ReportAPIError):
        retrieve_api_report(bundle, '링네트', 2025, '주요 사업')
    with pytest.raises(ReportAPINoData):
        retrieve_api_report(None, '이삭엔지니어링', 2025, '주요 사업')


def test_pagination_includes_reports_on_later_pages():
    calls = []
    def get(url, *, params, **kwargs):
        calls.append(params)
        rows = [filing(year=2024)] if params['page_no'] == '1' else [filing(year=2025)]
        return Response({'status': '000', 'total_page': 2, 'list': rows})
    rows = ReportAPIClient(KEY, session=SimpleNamespace(get=get)).filings('이삭엔지니어링')
    assert {_year(r) for r in rows} == {2024, 2025}
    assert len(calls) == 2
