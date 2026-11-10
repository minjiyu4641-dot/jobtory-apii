"""Jobtory workspace presentation: real saved data, no model calls on render."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import plotly.graph_objects as go
import streamlit as st

from app_modules.presentation import money


GREEN = '#267B57'
ORANGE = '#EE8C35'
BROWN = '#3B291B'
CREAM = '#FBF8F2'
COLORS = [GREEN, ORANGE, '#EFC299', '#8DAF9C', '#B08B69', '#C2C8BB', '#D7B79C']

CSS = '''<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700;800&display=swap');
:root { --jt-ink:#3b291b; --jt-muted:#8a7c6e; --jt-line:#e9e0d5; --jt-green:#267b57; }
.stApp { background:#fbf8f2; color:var(--jt-ink); }
.stApp, input, textarea, button { font-family:'Noto Sans KR','Malgun Gothic',sans-serif; }
[data-testid="stHeader"] { display:none; }
[data-testid="stMainBlockContainer"] { max-width:1880px; padding:1.2rem 1.8rem 2rem; }
[data-testid="stSidebar"] { display:none; }
footer, #MainMenu { visibility:hidden; }
.stApp p { word-break:keep-all; overflow-wrap:anywhere; }
[data-testid="stCaptionContainer"] { color:var(--jt-muted); font-size:12px; }
.st-key-workspace_header { background:#fff; border-bottom:1px solid var(--jt-line); padding:10px 0 15px; margin-bottom:4px; }
.jt-brand { font-size:29px; line-height:42px; font-weight:800; letter-spacing:-1.5px; color:#351e10; white-space:nowrap; }
.jt-brand span { color:#e88c36; }
.jt-brand .jt-version { display:inline-block; color:#267b57; background:#edf6f0; border:1px solid #d4e5da; border-radius:5px; padding:3px 6px; font-size:11px; letter-spacing:0; line-height:1.2; vertical-align:middle; }
.jt-date { font-size:12px; color:var(--jt-muted); line-height:1.7; padding-top:6px; }
[data-testid="stSelectbox"] [data-baseweb="select"]>div { background:#fff; border:1px solid #dfd3c5; border-radius:8px; min-height:39px; }
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button, [data-testid="stPopover"]>button { border-color:#dfd4c7; border-radius:8px; background:#fff; color:#594534; }
[data-testid="stButton"] button[kind="primary"] { background:var(--jt-green); border-color:var(--jt-green); color:#fff; }
[data-testid="stButton"] button:hover, [data-testid="stDownloadButton"] button:hover { color:var(--jt-green); border-color:#8faf9b; }
.st-key-company_card, [class*="st-key-jt_kpi_"], .st-key-revenue_card, .st-key-segment_card, .st-key-customer_card, .st-key-research_card, .st-key-ai_panel { background:#fff; border:1px solid var(--jt-line)!important; border-radius:13px; box-shadow:none; }
.st-key-company_card, .st-key-revenue_card, .st-key-segment_card, .st-key-customer_card, .st-key-research_card { padding:17px 19px 15px; }
[class*="st-key-jt_kpi_"] { padding:12px 13px 13px; min-height:133px; }
.jt-eyebrow { font-size:12px; color:#8c7763; margin-bottom:8px; font-weight:600; }
.jt-company { font-size:25px; line-height:1.35; font-weight:800; letter-spacing:-.8px; margin-bottom:8px; }
.jt-overview { font-size:13px; color:#685747; line-height:1.8; margin:0; }
.jt-tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:11px; }
.jt-tag { border-radius:999px; padding:4px 10px; background:#fbf0e4; color:#84664b; font-size:11px; }
.jt-title { font-size:15px; line-height:1.7; color:#44301f; font-weight:700; margin:0; }
.jt-small { font-size:11px; color:#998775; line-height:1.65; margin-top:3px; }
.jt-kpi-label { font-size:12px; color:#8d7a66; line-height:2; }
.jt-kpi-value { font-size:clamp(18px,1.65vw,25px); line-height:1.5; font-weight:750; letter-spacing:-.65px; font-variant-numeric:tabular-nums; color:#3e3023; white-space:nowrap; }
.jt-kpi-foot { font-size:10.5px; color:#97836f; line-height:1.7; margin-top:6px; }
.jt-up { color:#c54b3a; }.jt-down { color:#3f6da4; }
[class*="st-key-ask_"] button { font-size:11px; padding:3px 9px; min-height:29px; white-space:nowrap; background:#fffcf8; }
.jt-excerpt { font-size:12px; line-height:1.85; color:#6f5b49; margin:6px 0 10px; }
.jt-basis { font-size:10.5px; color:#9e8b78; line-height:1.7; margin-top:8px; }
.jt-empty { font-size:12px; line-height:1.8; color:#998b7b; background:#fcfaf6; border:1px dashed #e7ded2; border-radius:8px; padding:14px; margin:8px 0; }
.jt-footnote { font-size:11px; color:#9a8774; line-height:1.8; padding:4px 1px; }
.st-key-ai_panel { padding:18px 18px 16px; }
.jt-ai-title { font-size:18px; font-weight:750; color:#3b291b; line-height:1.8; }
.jt-badge { display:inline-block; font-size:10px; font-weight:600; padding:3px 7px; background:#edf6f0; color:#33845f; border-radius:5px; margin-left:6px; vertical-align:middle; }
.st-key-ai_panel [data-testid="stToggle"] label p { font-size:11px; white-space:nowrap; }
.st-key-ai_thread { border-top:1px solid #f0e9df; border-bottom:1px solid #f0e9df; padding:12px 1px 10px; }
.st-key-ai_panel [data-testid="stLayoutWrapper"]:has(> .st-key-ai_thread) { height:auto!important; flex:none; }
.st-key-ai_thread { height:clamp(380px,calc(100dvh - 330px),1050px)!important; max-height:clamp(380px,calc(100dvh - 330px),1050px)!important; min-height:0!important; flex:0 0 auto!important; }
.jt-welcome { background:#fff8ed; border:1px solid #f0dfc4; border-radius:11px; padding:14px; margin-bottom:8px; }
.jt-welcome strong { display:block; font-size:12px; color:#287d57; line-height:1.7; margin-bottom:8px; }
.jt-welcome ul { padding-left:17px; margin:0; }
.jt-welcome li { font-size:11.5px; color:#6f553d; line-height:1.85; }
.jt-user-question { margin:8px 0 12px auto; width:fit-content; max-width:94%; background:#267b57; color:#fff; border-radius:12px 12px 2px 12px; padding:10px 13px; font-size:12px; line-height:1.75; }
.jt-answer-label { font-size:11px; color:#8b755e; font-weight:700; margin:12px 0 7px; }
.jt-answer-label.green { color:#267b57; }
.st-key-ai_panel [data-testid="stMarkdownContainer"] p { font-size:15px; line-height:1.85; }
.st-key-ai_panel h3 { font-size:18px; }
[class*="st-key-preset_"] button { min-height:32px; padding:5px 9px; border-radius:999px; background:#fffdfa; font-size:11px; }
[class*="st-key-follow_"] button, [class*="st-key-answer_followup_"] button { min-height:30px; padding:6px 10px; text-align:left; justify-content:flex-start; font-size:11px; }
.st-key-ai_panel [data-testid="stTextArea"] textarea { border-radius:8px; font-size:14px; background:#fff; border:1px solid #e6ddd1!important; padding:12px; }
.st-key-ai_panel [data-testid="stExpander"] { border:1px solid #ede4d8; border-radius:9px; background:#fff; }
.st-key-ai_panel [data-testid="stExpander"] summary p { font-size:14px; }
.st-key-ai_panel [data-testid="stAlert"] { font-size:12px; }
.st-key-financial_details { --ink:#3b291b; --muted:#85715e; --line:#e9e0d5; --paper:#fbf8f2; --card-line:#e9e0d5; }
.st-key-financial_details .financial-basis { padding:0 2px; }
.st-key-financial_details .financial-basis .chips { margin-top:0; }
.st-key-financial_details .years-row { margin-top:9px; }
.st-key-financial_details .sec { padding-top:7px; }
.st-key-financial_details .summary { padding:17px 18px; }
.st-key-financial_details .summary-head { font-size:1.05rem; }
.st-key-financial_details .strip { grid-template-columns:repeat(3,1fr); }
.st-key-financial_details .kpi { height:120px; padding:13px 14px; }
.st-key-financial_details .kpi-value { font-size:clamp(1rem,1.5vw,1.5rem); }
.st-key-financial_details button[data-baseweb="tab"] { padding:.65rem .6rem; font-size:.85rem; }
@media(min-width:1050px){
  [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:has(.st-key-ai_panel){position:sticky;top:18px;align-self:flex-start;}
}
@media(max-width:1050px){
  [data-testid="stMainBlockContainer"]{padding:1rem;}
  .jt-kpi-value{font-size:20px;}
}
@media(max-width:700px){
  [data-testid="stMainBlockContainer"]{padding:.5rem .7rem 2rem;}
  .jt-brand{font-size:25px;}
  .jt-company{font-size:22px;}
  .st-key-ai_thread{height:520px!important;max-height:520px!important;}
}
</style>'''


def esc(value) -> str:
    return html.escape(str(value if value is not None else ''))


def apply_workspace_theme():
    st.html(CSS)


def clipped(text: str, size: int = 360) -> str:
    clean = ' '.join(text.split())
    return clean if len(clean) <= size else clean[:size].rsplit(' ', 1)[0] + '…'


def section_source(section) -> str:
    if section is None:
        return '해당 자료 미확보'
    if getattr(section, 'source_kind', '') == 'opendart_report':
        return f'OpenDART API · {section.fiscal_year} 사업연도 · {section.section} · 접수번호 {section.rcept_no}'
    if getattr(section, 'source_kind', '') == 'supabase_report':
        return f'Supabase · {section.fiscal_year} 사업연도 · {section.section} · 접수번호 {section.rcept_no}'
    return f'{section.section} · PDF {section.start_pdf_page}–{section.end_pdf_page}쪽'


def overview_text(sections) -> str:
    section = next((s for s in sections if s.category == 'business'), None)
    if section is None:
        return '사업 개요를 확인할 수 있는 공시 본문을 준비 중입니다.'
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', section.text) if len(p.strip()) >= 45]
    preferred = next((p for p in paragraphs if re.search(r'(당사|지배회사|회사는)', p)), None)
    return clipped(preferred or (paragraphs[0] if paragraphs else section.text), 290)


def topic_tags(sections, limit=4):
    text = '\n'.join(s.text for s in sections if s.category in ('business', 'products', 'research_development'))
    terms = ('AI', '클라우드', '스마트팩토리', '자동화', '전기차 충전', '디지털 전환', '네트워크', '플랫폼', '데이터센터', '시스템 구축', '보안')
    found = [(text.find(term), term) for term in terms if term in text]
    return [term for _, term in sorted(found)[:limit]]


def report_excerpt(sections, categories, terms, size=380):
    candidates = []
    for section in sections:
        if section.category not in categories:
            continue
        for part in re.split(r'\n\s*\n', section.text):
            clean = ' '.join(part.split())
            if len(clean) < 55:
                continue
            hits = sum(term in clean for term in terms)
            if hits:
                candidates.append((hits + int('당사' in clean), section, clean))
    if not candidates:
        return None, ''
    _, section, text = max(candidates, key=lambda row: row[0])
    return section, clipped(text, size)


@dataclass(frozen=True)
class EmployeeSummary:
    count: int | None = None
    tenure: str = ''
    average_salary_manwon: float | None = None
    source: str = ''


def employee_summary(sections) -> EmployeeSummary:
    """Read only an explicit total row; never average gender/business averages."""
    for section in sections:
        if section.category != 'employees':
            continue
        lines = section.text.splitlines()
        table = [[v.strip() for v in line.strip().strip('|').split('|')]
                 for line in lines if line.strip().startswith('|')]
        header = next((row for row in table if '직원 합계(명)' in row and '평균근속연수' in row), None)
        totals = [row for row in table if row and row[0] == '합계']
        if header is None or len(totals) != 1 or len(totals[0]) != len(header):
            continue
        row = dict(zip(header, totals[0]))
        count_text = row.get('직원 합계(명)', '').replace(',', '')
        count = int(count_text) if count_text.isdigit() else None
        tenure = row.get('평균근속연수', '')
        if tenure in ('', '-', '—'):
            tenure = ''
        salary = None
        unit = re.search(r'급여 단위\s*:\s*(백만원|천원|원)', section.text)
        amount = row.get('1인평균급여액', '').replace(',', '')
        if unit and re.fullmatch(r'\d+(?:\.\d+)?', amount):
            salary = float(amount) * {'백만원': 100, '천원': .1, '원': .0001}[unit.group(1)]
        return EmployeeSummary(count, tenure, salary, section_source(section))
    return EmployeeSummary()


def segment_rows(sections):
    """Extract explicitly printed percentages from three known table formats.

    A layout without an unambiguous percentage table shows source excerpts.
    Negative consolidation adjustments remain signed and are not normalized.
    """
    section = next((s for s in sections if s.category == 'products'), None)
    if section is None:
        return [], None
    text = section.text
    rows = []
    if section.corp_name == '롯데이노베이트':
        for line in text.splitlines():
            match = re.match(r'^\s*(SM|SI|전기차 충전)\s+.*?([\d,]+)\s+([\d.]+)%\s*$', line)
            if match:
                rows.append((match.group(1), float(match.group(3))))
    elif section.corp_name == '인성정보':
        labels = [('IT 인프라', 'IT 인프라'), ('헬스케어', '헬스케어'),
                  ('소프트웨어', '소프트웨어 도매'), ('네트워크장비', '네트워크장비 도매'), ('연결조정', '연결조정')]
        for needle, label in labels:
            start = text.find(needle)
            match = re.search(r'\((-?\d+(?:\.\d+)?)%\)', text[start:start+600]) if start >= 0 else None
            if match:
                rows.append((label, float(match.group(1))))
    elif section.corp_name == '이삭엔지니어링':
        block = text.split('2-1. 주요 제품등의 현황', 1)[-1].split('2-2.', 1)[0]
        for line in block.splitlines():
            match = re.match(r'^\s*(.*?)\s+(\d[\d,]*)\s+(\d+\.\d+)\s*(?:-|\s)*$', line)
            if match:
                label = re.sub(r'^(?:정보서비스 등|솔루션 제공|상품|기타)\s*', '', match.group(1)).strip()
                if label and '합계' not in label:
                    rows.append((label, float(match.group(3))))
    return rows, section


def ask_button(key, question, choose_question, parent='business'):
    st.button('물어보기', key=key, on_click=choose_question, args=(question, None, parent),
              help='오른쪽 질문창에 입력합니다. 답변 받기를 누르면 AI가 설명합니다.')


def card_heading(title, key, question, choose_question, parent='business'):
    title_col, button_col = st.columns([4, 1.1], vertical_alignment='center')
    with title_col:
        st.html(f'<div class="jt-title">{esc(title)}</div>')
    with button_col:
        ask_button(key, question, choose_question, parent)


def render_company_header(sections, company, choose_question, root, read_pdf):
    """Full-width company overview above the dashboard and question panel."""
    with st.container(border=True, key='company_card'):
        content, actions = st.columns([4.4, 1.25], gap='medium')
        with content:
            st.html(f'<div class="jt-eyebrow">기업 개요</div><div class="jt-company">{esc(company)}</div>'
                    f'<p class="jt-overview">{esc(overview_text(sections))}</p>'
                    '<div class="jt-tags">' + ''.join(f'<span class="jt-tag">{esc(t)}</span>' for t in topic_tags(sections)) + '</div>')
        with actions:
            if sections and getattr(sections[0], 'source_kind', '') in {'opendart_report', 'supabase_report'}:
                st.link_button('사업보고서 DART 원문',
                    f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={sections[0].rcept_no}', width='stretch')
            elif sections:
                from pathlib import Path
                pdf_name = Path(sections[0].source_file.replace('\\', '/')).name
                pdf_path = root / 'reports' / pdf_name
                if pdf_path.is_file():
                    st.download_button('사업보고서 PDF', read_pdf(str(pdf_path)), file_name=pdf_name,
                                       mime='application/pdf', key='overview_pdf', width='stretch')
            st.button('이 회사 물어보기', key='ask_overview', width='stretch', on_click=choose_question,
                      args=(f'{company}의 주요 사업·제품·고객과 실제로 어떤 일을 하는지 설명해줘.', None, 'business'))


def render_workspace_dashboard(bundle, sections, company, year, choose_question, root, read_pdf, detailed_renderer):
    if bundle is None:
        st.info('선택한 기업·연도의 저장 재무 분석이 없습니다. 공시 질문은 계속 사용할 수 있습니다.')
        return
    with st.container(key='financial_details'):
        detailed_renderer(bundle.data, bundle.meta, bundle.analysis,
                          on_ask=choose_question, show_company_header=False)
    st.download_button('재무 분석 JSON 내려받기', bundle.report_json(),
        file_name=f'{bundle.corp_code}_{year}_quantitative_report.json', mime='application/json', key='download_quant_report')

    left, right = st.columns(2, gap='small')
    with left, st.container(border=True, key='segment_card'):
        card_heading('사업부문별 매출 비중', 'ask_segments', '사업부문별 매출액과 비중을 비교하고 연결·별도 기준을 설명해줘.', choose_question, 'performance')
        rows, source = segment_rows(sections)
        if rows:
            st.html('<div class="jt-small">공시에 기재된 비중 · 연결조정은 부호 유지</div>')
            fig = go.Figure(go.Bar(x=[v for _, v in rows], y=[k for k, _ in rows], orientation='h',
                marker_color=[COLORS[i % len(COLORS)] for i in range(len(rows))],
                text=[f'{v:g}%' for _, v in rows], textposition='auto',
                hovertemplate='%{y} · %{x:g}%<extra></extra>'))
            fig.update_layout(height=max(170, len(rows)*29), margin=dict(l=5, r=6, t=15, b=5),
                paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                font=dict(family='Noto Sans KR, sans-serif', size=10, color='#765f49'),
                yaxis=dict(autorange='reversed'), xaxis=dict(ticksuffix='%', gridcolor='#f3eee7', zerolinecolor='#d1c4b4'))
            st.plotly_chart(fig, width='stretch', config={'displayModeBar':False}, key='workspace_segment_chart')
        else:
            source, excerpt = report_excerpt(sections, ('products', 'revenue_orders'), ('사업', '매출'), 260)
            st.html('<div class="jt-empty">비중을 확인할 수 있는 표는 원문에서 확인해 주세요.</div>')
            if excerpt:
                st.html(f'<div class="jt-excerpt">{esc(excerpt)}</div>')
        st.html(f'<div class="jt-basis">{esc(section_source(source))}</div>')
    with right, st.container(border=True, key='customer_card'):
        card_heading('주요 고객·판매 방식', 'ask_customers', '주요 고객과 판매 경로, 수주잔고를 공시 근거로 설명해줘.', choose_question, 'business')
        source, excerpt = report_excerpt(sections, ('revenue_orders', 'products', 'business'),
                                          ('고객', '판매', '직판', 'Dealer', '수주'), 420)
        if excerpt:
            st.html(f'<div class="jt-excerpt">{esc(excerpt)}</div>')
        else:
            st.html('<div class="jt-empty">관련 공시 원문을 질문으로 확인해 보세요.</div>')
        st.html(f'<div class="jt-basis">공시 원문 발췌 · {esc(section_source(source))}</div>')
    with st.container(border=True, key='research_card'):
        card_heading('연구개발·신사업', 'ask_research', '연구개발·신사업의 추진 단계와 실제 성과를 구분해 설명해줘.', choose_question, 'technology')
        st.html('<div class="jt-small">공시에 언급된 기술·사업 키워드</div>'
                '<div class="jt-tags">'+''.join(f'<span class="jt-tag">{esc(t)}</span>' for t in topic_tags(sections, 5))+'</div>')
        source, excerpt = report_excerpt(sections, ('research_development', 'business', 'management'),
                                         ('신사업', '연구개발', '개발', '신규'), 230)
        if excerpt:
            st.html(f'<div class="jt-excerpt">{esc(excerpt)}</div>')
        st.html(f'<div class="jt-basis">{esc(section_source(source))}</div>')
    st.html('<div class="jt-footnote">재무 수치는 저장된 DART 공시 분석 결과입니다. 직원·급여는 해당 법인 직원 현황의 공시 합계이며 연결 재무 수치와 집계 범위가 다를 수 있습니다.</div>')


def render_welcome(company, year, sections, bundle):
    bullets = [clipped(overview_text(sections), 155)]
    if bundle is not None:
        statement = bundle.data['financials'][str(year)]['statement']
        bullets.append(f'{year}년 매출액 {money(statement.get("revenue"))}, 영업이익 {money(statement.get("operating_income"))}이에요.')
        bullets.append('왼쪽 카드의 물어보기를 누르면 관련 질문을 준비해 드려요.')
    st.html(f'<div class="jt-welcome"><strong>🌰 잡토리가 모아온 {esc(company)}의 핵심 도토리</strong><ul>'
            + ''.join(f'<li>{esc(t)}</li>' for t in bullets) + '</ul></div>')
