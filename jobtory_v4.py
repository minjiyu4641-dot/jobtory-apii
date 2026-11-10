"""실행: python -m streamlit run jobtory_v4.py --server.port 8505"""
from __future__ import annotations

import os
import re
import hashlib
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from answer_service import AnswerError, generate_answer
from core import (CATEGORY_LABELS, FOLLOW_UP_QUESTIONS, QUESTION_BY_ID, QUESTION_CATEGORY_DESCRIPTIONS, QUESTIONS, DataError, build_index, coverage_notes,
                  load_reports, retrieve)
from paper_service import (PaperError, load_papers, make_paper_evidence, paper_links, save_papers,
                           search_nal)
from research_service import ResearchError, find_research, find_stored_research, select_papers
from hybrid_research import find_hybrid_research
from nal_rate_limit import NalQuota
from paper_repository import PaperDatabaseError, PaperRepository, papers_revision
from report_api import (API_COMPANIES, ReportAPIClient, ReportAPIError, ReportAPINoData,
                        report_document_name, retrieve_api_report, _year)
from paper_quality import eligible_papers, exclusion_reason
from dart_service import DartClient, DartError, DartNoData, cached_companies, save_employees
from dart_bridge import (DartDataError, connect_employee_evidence, dart_data_signature,
                         format_collected_at, load_employee_snapshot)


from financial_bridge import (FinancialDataError, load_financial_bundle,
                              connect_financial_evidence, financial_signature)
from app_modules.financial_dashboard import render_dashboard
from cursor_effect import render_cursor_squirrel
from workspace_layout import (apply_workspace_theme, render_company_header, render_workspace_dashboard, render_welcome,
                              overview_text, esc, clipped)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="Jobtory v5.0 | OpenDART API 공시 검색", page_icon="🔎", layout="wide", initial_sidebar_state="collapsed")
apply_workspace_theme()


def server_setting(name):
    value = os.getenv(name, '').strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, '')).strip()
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return ''


@st.cache_resource(show_spinner=False)
def database_repository(connection_id, _url, _key):
    return PaperRepository(_url, _key)


@st.cache_data(ttl=60, show_spinner=False)
def read_database_papers(connection_id, refresh_token, _url, _key):
    return database_repository(connection_id, _url, _key).load()


@st.cache_data(ttl=3600, show_spinner=False)
def read_api_filings(connection_id, refresh_token, _api_key, corp_name):
    return ReportAPIClient(_api_key).filings(corp_name)


@st.cache_data(ttl=3600, show_spinner=False)
def read_api_report(connection_id, refresh_token, _api_key, corp_name, fiscal_year, filings):
    return ReportAPIClient(_api_key).load(corp_name, fiscal_year, filings=filings)


def refresh_api_report():
    st.session_state['report_api_refresh'] = st.session_state.get('report_api_refresh', 0) + 1
    clear_result()
    st.session_state.pop('research_cache', None)


supabase_url = server_setting('SUPABASE_URL')
supabase_key = server_setting('SUPABASE_SECRET_KEY')
database_requested = bool(supabase_url or supabase_key)
database_error = None
paper_database = None
database_connection_id = hashlib.sha256((supabase_url + '|' + supabase_key).encode()).hexdigest()
if database_requested:
    try:
        paper_database = database_repository(database_connection_id, supabase_url, supabase_key)
    except PaperDatabaseError as exc:
        database_error = str(exc)


@st.cache_data(show_spinner=False)
def read_pdf(path: str) -> bytes:
    return Path(path).read_bytes()


def clear_result():
    for key in ("evidence", "paper_evidence", "answer_result", "coverage", "result_question", "result_corp", "result_year", "answer_error", "research_result", "research_error", "report_retrieval_counts"):
        st.session_state.pop(key, None)


def toggle_ai_panel():
    st.session_state['ai_panel_open'] = not st.session_state.get('ai_panel_open', True)


def refresh_database_papers(preserve_answer=False):
    st.session_state['paper_db_refresh'] = st.session_state.get('paper_db_refresh', 0) + 1
    st.session_state.pop('research_cache', None)
    if preserve_answer:
        st.session_state['paper_db_preserve_next_revision'] = True
    else:
        clear_result()


def persist_papers(papers):
    if database_requested:
        if paper_database is None:
            raise PaperDatabaseError(database_error or '논문 DB 연결 설정을 확인해 주세요.')
        result = paper_database.save(papers)
        refresh_database_papers()
        return result
    return save_papers(papers, ROOT / 'papers')


def quota_search_nal(query, api_key, **kwargs):
    quota = NalQuota(api_key, ROOT / 'data' / 'paper_collection', max_requests=20)
    return search_nal(query, api_key, requester=quota.post, **kwargs)


def choose_question(text: str, preset_id=None, parent_id=None):
    st.session_state['ai_panel_open'] = True
    st.session_state["question"] = text
    st.session_state["preset_id"] = preset_id
    if parent_id or preset_id:
        st.session_state["active_question_id"] = parent_id or preset_id
    clear_result()


def document_name(item) -> str:
    if getattr(item, "source_kind", "") == "opendart_report":
        return report_document_name(item)
    if getattr(item, "source_kind", "") == "financial_analysis":
        return f"{item.section} · {item.fs_div} · 공시 접수번호 {item.rcept_no or '미확보'}"
    if getattr(item, "source_kind", "") == "opendart":
        return f"{item.source_file} · 공시 접수번호 {item.rcept_no}"
    if hasattr(item, "source_file"):
        name = Path(item.source_file.replace("\\", "/")).name
        pages = str(item.start_pdf_page) if item.start_pdf_page == item.end_pdf_page else f"{item.start_pdf_page}–{item.end_pdf_page}"
        prefix = '저장 사업보고서 · ' if getattr(item, 'source_kind', '') == 'saved_report' else ''
        return f"{prefix}{name} · {item.section} · PDF {pages}쪽"
    return f"{item.title} ({item.publication_year or '연도 미제공'})"


def readable_text(text: str) -> str:
    # 본문에는 내부 ID나 긴 파일명을 반복하지 않습니다. 실제 근거는 아래에 모읍니다.
    return re.sub(r"\s*\[?(?:[SP][0-9a-f]{10}|Q\d{4})\]?", "", text).strip()


def display_claim(claim, evidence, papers=()):
    st.write(readable_text(claim.text))


def display_sources(evidence, cited_ids=None, usages=None):
    grouped = {}
    for item in evidence:
        if cited_ids is not None and item.source_id not in cited_ids:
            continue
        group = grouped.setdefault(item.source_file, {})
        group.setdefault(item.source_id, item)
    for source_file, group in grouped.items():
        with st.expander(Path(source_file.replace("\\", "/")).name):
            first_item = next(iter(group.values()))
            if getattr(first_item, "source_kind", "") == "opendart_report":
                st.caption("OpenDART API에서 받은 사업보고서 본문입니다. XML 위치는 PDF 페이지 번호가 아닙니다.")
                st.link_button("이 근거의 DART 공시 열기", first_item.source_url,
                               key=f"report_db_source_{first_item.source_id}")
            elif getattr(first_item, "source_kind", "") == "financial_analysis":
                st.caption("대시보드와 같은 저장 재무 분석 결과입니다. 금액은 원(KRW), 비율은 % 기준입니다.")
                if first_item.source_url:
                    st.link_button("재무 수치의 DART 원문 열기", first_item.source_url, key=f"financial_source_{first_item.source_id}")
                if first_item.benchmark_url:
                    st.link_button("한국은행 업종 통계", first_item.benchmark_url, key=f"financial_benchmark_{first_item.source_id}")
            elif getattr(first_item, "source_kind", "") == "opendart":
                st.caption("OpenDART 직원 현황 API 자료입니다. PDF 페이지 대신 공시 원문으로 연결합니다.")
                st.link_button("DART 원문 공시 열기", first_item.source_url, key=f"dart_source_{first_item.source_id}")
                st.caption("수집: " + format_collected_at(first_item.fetched_at))
            else:
                st.caption("페이지는 추출 절 전체의 범위입니다. 아래 발췌문의 정확한 한 페이지를 뜻하지 않습니다.")
            for item in group.values():
                st.caption(document_name(item))
                st.caption(f"{item.corp_name} · {item.fiscal_year} 사업연도")
                if getattr(item, 'source_kind', '') == 'opendart_report' and item.source_locator:
                    st.caption("본문 위치: " + item.source_locator)
                if usages and usages.get(item.source_id):
                    st.caption("연결된 설명: " + " / ".join(dict.fromkeys(usages[item.source_id])))
                st.text(item.text)


def display_paper_links(paper, location):
    links = paper_links(paper)
    for i, (label, url) in enumerate(links):
        st.link_button(label, url, key=f"paperlink_{location}_{paper.paper_id}_{i}")
    if not links:
        st.caption("제공 데이터에 논문 링크·DOI가 없습니다. 논문 제목으로 제공처에서 확인해 주세요.")
    else:
        st.caption("링크의 원문 열람에는 제공처 로그인·구독이 필요할 수 있습니다.")


def display_paper_sources(papers, cited_ids=None, usages=None):
    for paper in papers:
        if cited_ids is not None and paper.source_id not in cited_ids:
            continue
        year = paper.publication_year or "연도 미제공"
        with st.expander(f"{paper.title} · {year} · 논문 초록"):
            st.caption(f"저자: {', '.join(paper.authors) or '미제공'} · 학술지: {paper.journal or '미제공'}")
            st.caption("제공처: " + paper.provider)
            if usages and usages.get(paper.source_id):
                st.caption("연결된 설명: " + " / ".join(dict.fromkeys(usages[paper.source_id])))
            if paper.doi:
                st.caption("DOI: " + paper.doi)
            st.caption("초록에 제시된 범위의 설명입니다. 논문 전문이나 연구 원자료를 수집한 것이 아닙니다.")
            if paper.is_truncated:
                st.caption("분량 제한으로 초록의 앞부분만 전달했습니다. 저장 자료에는 수집한 전체 초록이 있습니다.")
            st.text(paper.text)
            display_paper_links(paper, "sources")


def display_research_details(research_result):
    with st.expander('논문 검색·선정 내역', expanded=False, key='paper_search_details'):
        if research_result:
            if research_result.from_cache:
                st.caption('동일 조건의 최근 검색·검토 결과를 재사용했습니다.')
            if research_result.search_counts:
                received = sum(research_result.search_counts.values())
                st.caption(f'이번 조회에서 받은 후보 {received}건 · 저장 후보 재검토 {research_result.saved_candidate_count}건 · 연결한 초록 {len(research_result.evidence)}편')
            if research_result.retrieval_mode:
                st.caption('검색 방식: ' + research_result.retrieval_mode)
                if research_result.retrieval_mode in ('DB 우선 + API 보충', 'Supabase DB'):
                    st.caption(f'DB에서 연결 {research_result.db_selected_count}편 · API에서 연결 {research_result.api_selected_count}편 · DB 추가 저장 {research_result.api_saved_count}편 · 기존 빈 항목 보완 {research_result.api_updated_count}편')
                if research_result.retrieval_mode == 'DB 우선 + API 보충':
                    period = '재사용한 검색의' if research_result.from_cache else '이번 검색의'
                    st.caption(f'{period} 국회도서관 API 실제 요청: {research_result.api_request_count}회')
                if research_result.api_status:
                    st.caption('API 보충 상태: ' + research_result.api_status)
                if research_result.api_note:
                    st.caption(research_result.api_note)
            if research_result.queries:
                st.caption('논문 검색어: ' + ' / '.join(research_result.queries))
            if research_result.excluded_reasons:
                details = ' · '.join(f'{reason} {count}건' for reason, count in research_result.excluded_reasons.items())
                st.caption('선정 내역: ' + details)
            for note in research_result.quality_notes:
                st.write(note)
        st.caption('초록·출처 링크·발행 시점과 질문 관련성을 검토한 자료만 최대 3편 표시합니다. 기업 사실은 공시로 확인합니다.')
        st.caption('연결 이유는 초록과 공시에 기반한 모델의 관련성 판단으로 오류가 있을 수 있습니다. 학술적 품질 보증이나 해당 기업의 기술 도입 확인을 뜻하지 않습니다.')


def display_research_section(answer, research_result, paper_evidence, evidence):
    st.subheader("함께 보는 논문 검색 결과")
    connections = getattr(research_result, "connections", {}) if research_result else {}
    supplied = {p.paper_id for p in paper_evidence}
    candidates = [p for p in (research_result.papers if research_result else [])
                  if p.paper_id in connections and p.paper_id in supplied and
                  not exclusion_reason(p, st.session_state.get("result_question", ""))]
    if not candidates:
        if st.session_state.get("research_error") or (research_result and research_result.warnings):
            st.info("논문 검색·관련성 검토를 완료하지 못했습니다. 위의 안내 문구를 확인해 주세요.")
        elif research_result and research_result.candidate_count:
            st.info("검색은 완료했지만 초록·출처 링크·발행 시점·질문 관련성 기준을 통과한 연구 자료를 확보하지 못했습니다.")
        elif research_result and research_result.queries:
            st.info("논문 검색은 정상 처리되었지만 시도한 검색어에 맞는 결과가 없습니다. 다른 연구 주제나 자료유형을 선택해 보세요.")
        elif research_result:
            st.info(research_result.plan.reason)
        else:
            st.info("이번 질문에 활용할 수 있는 연구 자료를 확보하지 못했습니다.")
    else:
        if answer and answer.research_background:
            st.markdown("**논문 초록에서 확인한 연구 내용**")
            for claim in answer.research_background:
                display_claim(claim, evidence, paper_evidence)
        for paper in candidates:
            connection = connections[paper.paper_id]
            with st.container(border=True):
                st.markdown("**" + paper.title + "**")
                if research_result and paper.paper_id in research_result.paper_origins:
                    st.caption('찾은 위치: ' + research_result.paper_origins[paper.paper_id])
                st.caption(f"{paper.publication_year} · 저자: {', '.join(paper.authors) or '미제공'} · {paper.journal or '학술지 정보 미제공'}")
                st.caption("제공처: " + paper.provider)
                st.markdown("**연결 이유**")
                st.write(readable_text(connection.reason))
                with st.expander("연결 근거 확인"):
                    report_context = [*evidence, *getattr(research_result, "report_context", [])]
                    linked = next((e for e in report_context if e.source_id == connection.report_source_id), None)
                    st.caption("공시 원문 · " + (document_name(linked) if linked else "사업·제품 설명 발췌"))
                    st.text(connection.report_quote)
                    st.caption("논문 초록 원문")
                    st.text(connection.abstract_quote)
                st.caption("초록 원문 발췌 · 답변 참고자료로 전달")
                st.write(paper.abstract[:420] + ("…" if len(paper.abstract) > 420 else ""))
                if len(paper.abstract) > 420:
                    with st.expander("초록 더 보기"):
                        st.text(paper.abstract)
                display_paper_links(paper, "results")
    display_research_details(research_result)


def render_settings(corp_name, fiscal_year, dart_directory, dart_snapshot, dart_warnings):
    st.caption('사업보고서 본문과 직원 현황 항목은 상단의 공시 API 연결에서 불러옵니다.')
    st.caption('재무 차트는 기존 저장 분석 자료이며, 공시 API 근거와 별도로 표시됩니다.')
    st.divider()
    env_openai_key = server_setting("OPENAI_API_KEY")
    if env_openai_key == "YOUR_OPENAI_API_KEY":
        env_openai_key = ""
    with st.expander("LLM 연결 설정", expanded=not bool(env_openai_key)):
        entered_key = st.text_input("OpenAI API 키", type="password", key="api_key_input",
                                    placeholder="설정된 키 사용 중 · 변경할 때만 입력" if env_openai_key else "API 키 입력",
                                    help="이 창에 입력한 키는 파일에 저장하지 않습니다. 키를 채팅이나 코드에 붙이지 마세요.")
        model = st.text_input("모델", value=server_setting("OPENAI_MODEL") or "gpt-4.1-mini", key="model", on_change=clear_result)
        st.caption("키는 이 입력창 또는 앱 폴더의 .env에 설정하세요.")
    api_key = (entered_key or env_openai_key).strip()
    if api_key == "YOUR_OPENAI_API_KEY":
        api_key = ""
    st.caption("LLM 연결 준비됨" if api_key else "키 없이도 '원문 먼저 확인'을 사용할 수 있어요.")
    st.caption("실제 답변 요청 시 선택된 보고서 발췌문을 OpenAI API에 전송합니다.")
    with st.expander("국회도서관 설정·직접 검색"):
        env_nal_key = server_setting("NAL_API_KEY")
        if env_nal_key == "YOUR_NAL_API_KEY":
            env_nal_key = ""
        nal_entered = st.text_input("국가학술정보 API 키", type="password", key="nal_api_key_input",
                                    placeholder="설정된 키 사용 중 · 변경할 때만 입력" if env_nal_key else "API 키 입력")
        nal_key = (nal_entered or env_nal_key).strip()
        if nal_key == "YOUR_NAL_API_KEY":
            nal_key = ""
        st.caption("국가학술정보 키 설정됨 · 다시 입력할 필요 없어요." if nal_key else "논문 자동검색에 사용할 키를 설정해 주세요.")
        st.caption("오른쪽 질문에서 자동검색할 수 있어요. 아래 직접 검색은 선택 사항입니다.")
        st.text_input("논문 검색어", placeholder="예: 스마트팩토리, 설비 이상탐지", key="paper_query", max_chars=100)
        paper_type = st.selectbox("자료유형", ["ARTICLE", "THESIS"], key="paper_type",
                                  format_func=lambda v: {"ARTICLE": "학술기사 · ARTICLE", "THESIS": "학위논문 · THESIS"}[v])
        paper_count = st.number_input("검색 건수", min_value=1, max_value=10, value=5, step=1, key="paper_count")
        search_clicked = st.button("국회도서관 논문 검색·저장", key="paper_search", disabled=not bool(nal_key))
        st.caption("국가학술정보(LOSI)의 통합검색·상세보기 활용 승인이 필요합니다.")
        st.caption("공식 안내의 HTTP API를 사용합니다. 키는 파일에 저장하지 않습니다.")
        if search_clicked:
            try:
                with st.spinner("국회도서관 검색·초록 상세조회 중이에요…"):
                    detail_progress = st.empty()
                    found = quota_search_nal(st.session_state["paper_query"], nal_key, count=int(paper_count),
                                       div_flag=paper_type,
                                       progress=lambda done, total: detail_progress.caption(f"초록 상세조회 {done}/{total}"))
                    persist_papers(found)
                    detail_progress.empty()
                clear_result()
                if found:
                    st.success(f"{len(found)}건 {'Supabase DB' if database_requested else 'JSON'} 저장 · 초록 제공 {sum(bool(p.abstract.strip()) for p in found)}건")
                else:
                    st.info("검색 결과가 없습니다. 검색어나 자료유형을 바꿔 보세요.")
            except (PaperError, OSError) as exc:
                st.error(str(exc) if isinstance(exc, PaperError) else "논문을 저장하지 못했습니다. 폴더 쓰기 권한을 확인하세요.")
    st.divider()
    return api_key, nal_key, model, dart_snapshot

# 이전 버전의 미검토 논문·캐시가 새 화면에 남지 않도록 결과만 초기화합니다.
if st.session_state.get("paper_quality_version") != "verified-connections-api-v3":
    clear_result()
    st.session_state.pop("research_cache", None)
    st.session_state["paper_quality_version"] = "verified-connections-api-v3"


# 카테고리 개편 이전의 질문·분석 결과가 새 버튼과 섞이지 않게 한 번만 초기화합니다.
if st.session_state.get("question_catalog_version") != "career-six-categories-v1":
    clear_result()
    st.session_state.update(question_catalog_version="career-six-categories-v1",
        active_question_id="business", preset_id="business", question=QUESTIONS[0].text)

if st.session_state.get("data_source_version") != "opendart-report-api-v5":
    clear_result()
    st.session_state.pop('research_cache', None)
    st.session_state["data_source_version"] = "opendart-report-api-v5"


# Resolve the API key before loading reports, including Streamlit Cloud Secrets.
with st.expander('공시 API 연결', expanded=not bool(server_setting('DART_API_KEY') or st.session_state.get('report_api_key_input'))):
    st.text_input('OpenDART API 키', type='password', key='report_api_key_input',
                  placeholder='서버에 설정된 키 사용 중' if server_setting('DART_API_KEY') else 'DART_API_KEY 입력',
                  on_change=refresh_api_report,
                  help='서버 .env 또는 Streamlit Secrets에 DART_API_KEY를 설정해도 됩니다. 입력값은 이 세션에서만 사용합니다.')
    st.caption('선택 기업의 사업보고서 원문을 OpenDART에서 가져옵니다.')
report_api_key = (st.session_state.get('report_api_key_input', '') or server_setting('DART_API_KEY')).strip()
report_connection_id = hashlib.sha256(report_api_key.encode()).hexdigest()
companies = sorted(API_COMPANIES)
report_bundle = None
report_load_error = None
report_data_notice = None
report_filings = []
report_api_selected = True
signature = 'opendart-report-api-v5'
with st.container(key="workspace_header"):
    brand_col, company_col, year_col, date_col, guide_col, settings_col = st.columns(
        [1.2, 1.65, 1.3, 2.25, .8, .65], gap="small", vertical_alignment="center")
    with brand_col:
        st.html('<div class="jt-brand">J<span>o</span>btory <span class="jt-version">v5.0</span></div>')
    with company_col:
        corp_name = st.selectbox("기업 선택", companies, key="company", on_change=clear_result,
                                 label_visibility="collapsed")
    with year_col:
        from datetime import date
        years = [date.today().year - 1]
        try:
            report_filings = read_api_filings(report_connection_id, st.session_state.get('report_api_refresh', 0),
                                              report_api_key, corp_name)
            api_years = sorted({_year(row) for row in report_filings if _year(row) is not None}, reverse=True)
            if api_years:
                years = api_years
            else:
                report_data_notice = '이 기업의 최근 사업보고서가 OpenDART API에서 조회되지 않았습니다.'
        except ReportAPIError as exc:
            report_load_error = str(exc)
        if st.session_state.get('year') not in years:
            st.session_state['year'] = years[0]
        fiscal_year = st.selectbox('보고서 사업연도', years, key='year', on_change=clear_result,
                                  format_func=lambda y: f'{y} 사업보고서', label_visibility='collapsed')
    if report_filings:
        try:
            with st.spinner('OpenDART에서 사업보고서 원문을 불러오고 있어요…'):
                report_bundle = read_api_report(report_connection_id, st.session_state.get('report_api_refresh', 0),
                                                report_api_key, corp_name, fiscal_year, report_filings)
            report_load_error = None
        except ReportAPINoData as exc:
            report_data_notice = str(exc)
        except ReportAPIError as exc:
            report_load_error = str(exc)
    # API report is the only source for the company cards and company Q&A.
    answer_index = report_bundle.index if report_bundle else []
    company_sections = report_bundle.sections if report_bundle else []
    report_source_label = 'OpenDART API' if report_bundle else '공시 API 자료 미확보'
    dart_directory = ROOT / "dart_data"
    dart_snapshot, dart_warnings = load_employee_snapshot(dart_directory, corp_name, fiscal_year)
    with date_col:
        basis = f'{fiscal_year} 사업연도 · API 공시 미확보'
        if report_bundle:
            basis = f'{report_bundle.filing_date} 제출 · OpenDART API 사업보고서'
        st.html(f'<div class="jt-date">자료 기준: {esc(basis)}</div>')
    with guide_col:
        with st.popover("이용 가이드"):
            st.write("기업을 고르고 왼쪽의 수치·공시 내용을 살펴보세요.")
            st.write("카드의 물어보기는 질문만 입력합니다. 오른쪽에서 원문을 확인하거나 AI 답변 받기를 누르세요.")
            st.caption("현재 화면: v5.0 · OpenDART API 공시 검색 · AI 패널 접기/펼치기")
            st.caption("선택 기업·사업연도의 최종 사업보고서를 API에서 조회하고 원문 ZIP/XML을 추출합니다.")
            st.caption("공시 API를 불러오지 못하면 상태를 표시합니다. 공시 API 새로 불러오기로 다시 시도할 수 있어요.")
            st.caption("재무 수치는 저장 분석, 기업 사실은 공시, 논문은 연구 배경 자료로 구분합니다.")
    with settings_col:
        with st.popover("⚙", help="API 연결 및 자료 설정"):
            st.markdown("**연결 설정**")
            api_key, nal_key, model, dart_snapshot = render_settings(
                corp_name, fiscal_year, dart_directory, dart_snapshot, dart_warnings)
            st.divider()
            st.toggle('🐿️ 다람쥐 마우스 커서', value=True, key='cursor_squirrel_enabled')

report_revision = (report_connection_id, corp_name, fiscal_year,
                   (report_bundle.rcept_no, report_bundle.revision) if report_bundle else (report_load_error, report_data_notice), signature)
if st.session_state.get('report_source_revision') != report_revision:
    clear_result()
    st.session_state.pop('research_cache', None)
    st.session_state['report_source_revision'] = report_revision

financial_bundle = None
financial_error = None
try:
    financial_bundle = load_financial_bundle(ROOT, corp_name, fiscal_year)
except FinancialDataError as exc:
    financial_error = str(exc)
current_financial_signature = financial_signature(ROOT)
if st.session_state.get("financial_source_signature") != current_financial_signature:
    clear_result()
    st.session_state["financial_source_signature"] = current_financial_signature

paper_load_error = None
database_truncated = False
try:
    if database_requested:
        if paper_database is None:
            raise PaperDatabaseError(database_error or '논문 DB 연결 설정을 확인해 주세요.')
        batch = read_database_papers(database_connection_id, st.session_state.get('paper_db_refresh', 0),
                                     supabase_url, supabase_key)
        paper_records, database_truncated = batch.papers, batch.truncated
    else:
        paper_records = load_papers(ROOT / "papers")
except PaperError as exc:
    paper_records = []
    paper_load_error = str(exc)
current_paper_revision = papers_revision(paper_records)
if st.session_state.get('paper_source_revision') != (database_connection_id, current_paper_revision):
    if not st.session_state.pop('paper_db_preserve_next_revision', False):
        clear_result()
    st.session_state.pop('selected_paper_ids', None)
    st.session_state['paper_source_revision'] = (database_connection_id, current_paper_revision)
selectable_papers, _ = eligible_papers(paper_records, st.session_state.get("question", QUESTIONS[0].text))
paper_by_id = {p.paper_id: p for p in selectable_papers}

render_company_header(company_sections, corp_name, choose_question, ROOT, read_pdf)

ai_panel_open = st.session_state.setdefault('ai_panel_open', True)
_, ai_controls = st.columns([1.25, 1], gap='medium')
with ai_controls:
    st.button('AI 질문하기 접기' if ai_panel_open else 'AI 질문하기 펼치기',
              key='toggle_ai_panel', on_click=toggle_ai_panel, width='stretch')
if ai_panel_open:
    dashboard_col, ai_col = st.columns([1.25, 1], gap="medium", vertical_alignment="top")
else:
    dashboard_col, ai_col = st.container(key='dashboard_full_width'), None
with dashboard_col:
    if report_api_selected:
        st.caption('재무 차트·조건 점검: 기존 저장 재무 분석 JSON · 기업 설명 카드: ' + report_source_label)
    render_workspace_dashboard(financial_bundle, company_sections, corp_name, fiscal_year,
                               choose_question, ROOT, read_pdf, render_dashboard)
    if financial_error:
        st.caption(financial_error)

if not ai_panel_open:
    # Keep hidden input values across Streamlit's widget cleanup; retain the answer too.
    for state_key in ('question', 'include_papers', 'research_mode', 'auto_paper_source',
                      'api_supplement_enabled', 'auto_paper_type', 'selected_paper_ids'):
        if state_key in st.session_state:
            st.session_state[state_key] = st.session_state[state_key]
    render_cursor_squirrel(bool(st.session_state.get('cursor_squirrel_enabled', True)))
    st.stop()

with ai_col, st.container(border=True, key="ai_panel"):
    ai_title, paper_toggle = st.columns([1.5, 1], vertical_alignment="center")
    with ai_title:
        st.html('<div class="jt-ai-title">AI에게 질문하기<span class="jt-badge">공시 근거 우선</span></div>')
    with paper_toggle:
        use_papers = st.toggle("논문 함께 보기", value=True, key="include_papers", on_change=clear_result)
    with st.container(height=750, border=False, key="ai_thread"):
        if report_bundle:
            st.success(f'공시 API 연결됨 · {corp_name} · {fiscal_year}년 · 본문 {len(report_bundle.sections)}개 항목')
            st.caption(f'공시 접수번호 {report_bundle.rcept_no} · OpenDART API 원문')
            st.caption('수집: ' + format_collected_at(report_bundle.fetched_at) + ' · API 결과는 최대 1시간 재사용합니다.')
            st.link_button('사업보고서 DART 원문 열기',
                f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={report_bundle.rcept_no}', key='api_report_source')
        else:
            st.warning(report_data_notice or report_load_error or '공시 API 원문을 불러오지 못했습니다.')
            st.caption('상단 공시 API 연결에서 키를 설정하고 새로 불러오기를 눌러 주세요.')
        st.button('공시 API 새로 불러오기', key='report_api_reload', on_click=refresh_api_report)
        if database_requested:
            st.caption('논문 Supabase DB 조회 실패' if paper_load_error else f'논문 Supabase DB 연결됨 · 불러온 논문 {len(paper_records)}편')
        if paper_load_error:
            st.warning(paper_load_error)
        if nal_key:
            try:
                st.caption(NalQuota(nal_key, ROOT / 'data' / 'paper_collection').status().message)
            except PaperError as exc:
                st.caption(str(exc))
        if "evidence" not in st.session_state:
            render_welcome(corp_name, fiscal_year, company_sections, None if report_api_selected else financial_bundle)
        with st.expander("추천 질문 · 6개 카테고리", expanded="evidence" not in st.session_state):
            active_id = st.session_state.setdefault("active_question_id", "business")
            if active_id not in QUESTION_BY_ID:
                active_id = "business"
            for start in (0, 3):
                for column, item in zip(st.columns(3, gap="small"), QUESTIONS[start:start+3]):
                    column.button(item.label, key=f"preset_{item.id}", width="stretch",
                        type="primary" if item.id == active_id else "secondary",
                        on_click=choose_question, args=(item.text, item.id))
            for i, text in enumerate(FOLLOW_UP_QUESTIONS[active_id]):
                st.button(text, key=f"follow_{active_id}_{i}", width="stretch", on_click=choose_question,
                          args=(text, None, active_id))
        selected_paper_ids = []
        auto_paper_type = "ARTICLE"
        auto_paper_source = 'DB 우선 + API 보충' if database_requested else '국회도서관 API'
        api_supplement_enabled = True
        if st.session_state.get('paper_mode_version') != '4.3':
            st.session_state.pop('auto_paper_source', None)
            st.session_state['paper_mode_version'] = '4.3'
        with st.expander("논문 활용 설정 · 기본은 질문으로 자동검색"):
            if database_requested:
                st.button('DB 논문 새로고침', key='paper_db_reload', on_click=refresh_database_papers)
                if database_truncated:
                    st.caption('직접 선택 목록은 최대 2,000편입니다. DB 우선 검색은 전체 papers 테이블에서 질문 관련 후보를 조회합니다.')
            research_mode = st.radio("논문 찾는 방법", ["질문으로 자동검색", "저장된 논문 직접 선택"],
                                     key="research_mode", on_change=clear_result, disabled=not use_papers)
            if research_mode == "질문으로 자동검색":
                if database_requested:
                    auto_paper_source = st.selectbox('자동검색 대상', ['DB 우선 + API 보충', 'Supabase DB', '국회도서관 API'],
                        key='auto_paper_source', on_change=clear_result, disabled=not use_papers)
                    if auto_paper_source == 'DB 우선 + API 보충':
                        api_supplement_enabled = st.toggle('DB 부족 시 API로 보충', value=True,
                            key='api_supplement_enabled', on_change=clear_result, disabled=not use_papers)
                        st.caption('DB에서 검토를 통과한 논문이 3편 미만이거나 최신 연구를 요청하면 API로 보충합니다. 기존 ID의 빈 초록도 제한적으로 재조회하고, 다음 페이지에서 새 후보를 찾습니다. 질문당 API 최대 8회·시간당 기존 한도를 유지합니다.')
                    elif auto_paper_source == 'Supabase DB':
                        st.caption('현재는 DB만 검색합니다. 부족할 때 API도 사용하려면 DB 우선 + API 보충으로 바꿔 주세요.')
                auto_paper_type = st.selectbox("자동검색 자료유형", ["ARTICLE", "THESIS"], key="auto_paper_type",
                                               format_func=lambda v: {"ARTICLE": "학술기사", "THESIS": "학위논문"}[v],
                                               on_change=clear_result, disabled=not use_papers)
                st.caption(f"{auto_paper_source}에서 질문과 공시의 관련 사업에 맞는 논문을 찾습니다. 초록·링크·발행 시점과 질문 관련성을 확인한 논문만 최대 3편 연결합니다.")
                st.caption("논문 활용을 켜면 검색어 판단과 초록 관련성 검토를 위한 모델 요청이 추가됩니다. 원문 미리보기는 모델·국회도서관 검색을 호출하지 않습니다.")
            else:
                st.caption(f"현재 저장된 논문 {len(paper_records)}편")
                selected_paper_ids = st.multiselect(
                    "관련 논문 선택 · 최대 3편", options=list(paper_by_id),
                    format_func=lambda pid: f"{paper_by_id[pid].title} ({paper_by_id[pid].publication_year or '연도 미제공'})",
                    max_selections=3, key="selected_paper_ids", on_change=clear_result, disabled=not use_papers,
                )
                if not paper_records:
                    st.caption("질문으로 자동검색하거나 왼쪽 직접 검색으로 논문을 추가할 수 있어요.")
                if len(paper_records) > len(paper_by_id):
                    st.caption("초록·출처 링크·발행 시점 기준을 통과한 저장 논문만 선택할 수 있습니다. 답변 요청 시 질문 관련성도 검토합니다.")
            st.caption("매출·법인·채용 등 기업 사실은 공시 근거로 설명합니다. 논문은 기술·연구 배경에 활용합니다.")

        response_slot = st.container()
    st.text_area("이 회사에 대해 궁금한 점을 입력하세요", key="question", height=120,
                 max_chars=2000, on_change=clear_result, label_visibility="collapsed",
                 placeholder="이 회사에 대해 궁금한 점을 입력하세요")
    preview_col, ask_col = st.columns([1, 1.2])
    preview_clicked = preview_col.button("원문 먼저 확인", key="preview", width="stretch", disabled=not bool(answer_index))
    ask_clicked = ask_col.button("AI 답변 받기", type="primary", key="ask", width="stretch",
                                disabled=not bool(api_key and model.strip() and answer_index))
    if not api_key:
        st.caption("상단 ⚙에서 API 키를 설정하면 AI 답변을 받을 수 있어요.")
    if preview_clicked or ask_clicked:
        clear_result()
        question = st.session_state["question"].strip()
        try:
            evidence, notes = retrieve_api_report(report_bundle, corp_name, fiscal_year,
                                                   question, st.session_state.get('preset_id'))
            st.session_state['report_retrieval_counts'] = (len(evidence), 0)
            paper_evidence = []
            if use_papers and ask_clicked:
                with st.spinner("질문에 맞는 논문과 연결 이유를 확인하고 있어요…"):
                    research_progress = st.empty()
                    try:
                        business_context = []
                        for category in ("business", "products"):
                            item = next((e for e in answer_index if e.corp_name == corp_name and e.fiscal_year == fiscal_year
                                         and e.category == category), None)
                            if item:
                                business_context.append(item)
                        seen = {e.source_id for e in business_context}
                        research_context = business_context + [e for e in evidence if e.source_id not in seen]
                        if research_mode == "저장된 논문 직접 선택":
                            research_result = select_papers(
                                papers=[paper_by_id[pid] for pid in selected_paper_ids if pid in paper_by_id],
                                question=question, corp_name=corp_name, fiscal_year=fiscal_year,
                                evidence=research_context, api_key=api_key, model=model,
                            )
                            research_result.retrieval_mode = '저장된 논문 직접 선택'
                            research_result.api_status = '자동검색 미사용'
                            research_result.api_note = '직접 선택한 논문만 검토합니다. API 보충은 질문으로 자동검색에서 사용할 수 있습니다.'
                        else:
                            if auto_paper_source == 'DB 우선 + API 보충':
                                quota = NalQuota(nal_key, ROOT / 'data' / 'paper_collection') if nal_key else None
                                research_result = find_hybrid_research(
                                    papers=paper_records, repository=paper_database,
                                    question=question, corp_name=corp_name, fiscal_year=fiscal_year,
                                    evidence=research_context, api_key=api_key, nal_key=nal_key,
                                    model=model, div_flag=auto_paper_type, quota=quota,
                                    api_enabled=api_supplement_enabled,
                                    cache=st.session_state.setdefault('research_cache', {}), progress=research_progress.caption)
                                if research_result.api_saved_count or research_result.api_updated_count:
                                    refresh_database_papers(preserve_answer=True)
                            elif auto_paper_source == 'Supabase DB':
                                if paper_load_error:
                                    raise ResearchError('Supabase DB 연결을 확인한 뒤 다시 질문해 주세요. 이번에는 공시 근거를 사용합니다.')
                                research_result = find_stored_research(
                                    papers=paper_records, question=question, corp_name=corp_name,
                                    fiscal_year=fiscal_year, evidence=research_context, api_key=api_key,
                                    model=model, div_flag=auto_paper_type,
                                    cache=st.session_state.setdefault('research_cache', {}), progress=research_progress.caption)
                            else:
                                research_result = find_research(
                                    question=question, corp_name=corp_name, fiscal_year=fiscal_year, evidence=research_context,
                                    api_key=api_key, nal_key=nal_key, model=model, div_flag=auto_paper_type,
                                    directory=ROOT / "papers", cache=st.session_state.setdefault("research_cache", {}),
                                    progress=research_progress.caption, search=quota_search_nal,
                                )
                                research_result.retrieval_mode = '국회도서관 API'
                                if database_requested and research_result.papers:
                                    try:
                                        persist_papers(research_result.papers)
                                    except PaperDatabaseError as exc:
                                        research_result.warnings.append(str(exc))
                        st.session_state["research_result"] = research_result
                        paper_evidence = research_result.evidence
                        notes.extend(research_result.warnings)
                        notes.extend(research_result.quality_notes)
                        if not paper_evidence and (research_result.queries or research_result.candidate_count):
                            notes.append("논문 선정 기준을 통과한 초록을 확보하지 못했습니다. 최근 산업 동향이나 매출 변동 원인을 논문으로 확인했다고 설명하지 마세요.")
                    except ResearchError as exc:
                        st.session_state["research_error"] = str(exc)
                        notes.append("논문 검색·연결 검토를 완료하지 못해 공시 근거만 전달했습니다.")
                    finally:
                        research_progress.empty()
            if paper_evidence:
                notes.append("논문 근거는 검색·선별한 논문의 초록입니다. 해당 연구와 선택 기업의 실제 사업은 별도로 확인해야 합니다.")
            st.session_state.update(evidence=evidence, paper_evidence=paper_evidence, coverage=notes, result_question=question,
                                    result_corp=corp_name, result_year=fiscal_year)
            if ask_clicked:
                with st.spinner("관련 공시 근거를 바탕으로 답변을 작성하고 있어요…"):
                    st.session_state["answer_result"] = generate_answer(
                        question=question, corp_name=corp_name, fiscal_year=fiscal_year,
                        evidence=evidence, notes=notes, api_key=api_key, model=model,
                        paper_evidence=paper_evidence,
                    )
        except (ValueError, AnswerError) as exc:
            st.session_state["answer_error"] = str(exc)
    with response_slot:
        if st.session_state.get('report_retrieval_counts'):
            api_count, _ = st.session_state['report_retrieval_counts']
            st.caption(f'이번 공시 근거: OpenDART API {api_count}개')
        if st.session_state.get("result_question"):
            st.html('<div class="jt-user-question">' + esc(st.session_state["result_question"]) + '</div>')
        if st.session_state.get("answer_error"):
            st.error(st.session_state["answer_error"])
        if st.session_state.get("research_error"):
            st.warning(st.session_state["research_error"] + " 공시 근거로 답변을 요청했습니다.")
        research_result = st.session_state.get("research_result")
        if research_result:
            for warning in research_result.warnings:
                st.warning(warning)

        evidence = st.session_state.get("evidence")
        paper_evidence = st.session_state.get("paper_evidence", [])
        result = st.session_state.get("answer_result")
        if evidence and any(getattr(e, "source_kind", "") == "opendart" for e in evidence):
            st.caption("DART 직원 현황을 답변 자료에 연결했습니다. 공시 원문 링크는 아래 ‘근거 모아보기’에서 확인할 수 있어요.")
        if evidence is not None:
            st.divider()
            if result:
                answer = result.answer
                with st.expander("공시 기반 답변", expanded=True):
                    if answer.status == "insufficient":
                        st.info(readable_text(answer.summary.text))
                    else:
                        display_claim(answer.summary, evidence, paper_evidence)
                    if answer.facts:
                        st.html('<div class="jt-answer-label">공시 원문 요약</div>')
                        for claim in answer.facts:
                            display_claim(claim, evidence, paper_evidence)
                    if answer.interpretations:
                        st.html('<div class="jt-answer-label green">AI 해석 · 취준생 관점</div>')
                        for claim in answer.interpretations:
                            display_claim(claim, evidence, paper_evidence)
                    if answer.follow_up_questions:
                        st.markdown("**지원 전에 확인할 질문**")
                        for i, item in enumerate(answer.follow_up_questions):
                            st.button(item + " →", key=f"answer_followup_{i}", on_click=choose_question, args=(item,))
                    if answer.limitations:
                        st.markdown("**확인되지 않은 내용**")
                        for item in answer.limitations:
                            st.write("• " + item)
                if use_papers:
                    display_research_section(answer, research_result, paper_evidence, evidence)
            elif evidence:
                st.subheader("검색한 원문")
                st.caption("아래는 실제 JSON에서 검색한 발췌문입니다. LLM이 생성한 답변이 아닙니다.")
            if not evidence:
                st.info("선택한 자료에서 이 질문에 필요한 근거를 찾지 못했습니다. 관련 보고서 절 또는 DART 직원 현황의 제공 범위를 확인해 주세요.")
            if not result and use_papers and (research_result or paper_evidence or st.session_state.get("research_error")):
                display_research_section(None, research_result, paper_evidence, evidence)
            with st.expander("자료 범위와 해석 기준"):
                if result:
                    st.caption(f"모델: {result.model} · 입력 {result.input_tokens:,} / 출력 {result.output_tokens:,} 토큰")
                for note in st.session_state.get("coverage", []):
                    st.write("• " + note)

        # 답변에 사용한 자료는 한곳에서 문서별로 중복을 제거해 표시합니다.
        usages = {}
        referenced_reports = []
        referenced_papers = []
        if result:
            answer = result.answer
            for label, claims in (("답변 요약", [answer.summary]), ("공시 사실", answer.facts),
                                  ("취준생 관점 해석", answer.interpretations), ("연구 배경", answer.research_background)):
                for number, claim in enumerate(claims, 1):
                    for sid in claim.source_ids:
                        usages.setdefault(sid, []).append(f"{label} {number}")
            referenced_reports.extend(item for item in (evidence or []) if item.source_id in usages)
            for item in evidence or []:
                if getattr(item, "source_kind", "") == "opendart" and item.source_id not in usages:
                    referenced_reports.append(item)
                    usages[item.source_id] = ["추가 조회 자료 · 이번 답변에서는 인용하지 않음"]
            referenced_papers.extend(item for item in paper_evidence if item.source_id in usages)
        elif evidence is not None:
            referenced_reports.extend(evidence)
            referenced_papers.extend(paper_evidence)
        if referenced_reports or referenced_papers:
            st.divider()
            st.subheader("근거 모아보기")
            st.caption("답변에서 인용한 공시·논문과 추가 조회한 DART 직원 현황입니다. 문서 이름을 열어 출처와 원문을 확인하세요.")
            display_sources(referenced_reports, usages=usages)
            display_paper_sources(referenced_papers, usages=usages)


render_cursor_squirrel(bool(st.session_state.get('cursor_squirrel_enabled', True)))


