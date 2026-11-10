"""제공된 Figma 디자인을 Streamlit의 실제 분석 화면에 적용하는 표현 계층."""
from __future__ import annotations

from html import escape
import base64
import re
from pathlib import Path

import streamlit as st

APERTURE = '''<svg xmlns="http://www.w3.org/2000/svg" width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="m8.2 4.4 3.1 5.4M19.4 9H13m5.3 8.1-3.1-5.4M10 20l3.2-5.5M4.6 15h6.3M5.7 6.9l3.1 5.4"/></svg>'''
def _svg_image(svg: str) -> str:
    encoded = base64.b64encode(svg.encode()).decode()
    return f'<img src="data:image/svg+xml;base64,{encoded}" width="23" height="23" alt="">'


APERTURE = _svg_image(APERTURE.replace('stroke="currentColor"', 'stroke="#bde9d2"'))
BUILDING = _svg_image('<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#2a745c" stroke-width="1.6"><path d="M4 21h16M6 21V5l6-2 6 2v16M9 8h.01M15 8h.01M9 12h.01M15 12h.01M9 16h.01M15 16h.01"/></svg>')


CATEGORY_ICONS = {
    "business": ":material/work_outline:", "technology": ":material/trending_up:",
    "performance": ":material/bar_chart:", "stability": ":material/verified_user:",
    "salary": ":material/payments:", "work_environment": ":material/groups:",
}

CSS = '''<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700;800&display=swap');
:root { color-scheme: light; --cl-green:#1e5b47; --cl-ink:#202520; --cl-line:#dce1db; }
html { scroll-behavior:smooth; }
.stApp { background:#f7f8f6; color:var(--cl-ink); }
.stApp, .stApp input, .stApp textarea, .stApp button { font-family:'Noto Sans KR','Apple SD Gothic Neo','Malgun Gothic',sans-serif; }
.stApp p { word-break:keep-all; overflow-wrap:anywhere; }
[data-testid="stHeader"] { background:rgba(247,248,246,.92); }
[data-testid="stMainBlockContainer"] { max-width:1170px; padding:4.4rem 3rem 4rem; }
[data-testid="stMain"] h3 { font-size:1.3rem; letter-spacing:-.04em; line-height:1.5; }
[data-testid="stCaptionContainer"] { color:#7a827c; font-size:.78rem; line-height:1.75; }
[data-testid="stSidebar"] { background:#fff; border-right:1px solid var(--cl-line); min-width:300px; max-width:330px; }
[data-testid="stSidebarContent"] { padding-top:.5rem; }
[data-testid="stSidebarUserContent"] { padding:1rem 1.25rem 2rem; }
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap:.85rem; }
[data-testid="stSidebar"] h3 { font-size:1.08rem; letter-spacing:-.03em; }
[data-testid="stSidebar"] label p { font-size:.8rem; font-weight:600; color:#465349; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] [data-baseweb="select"]>div { min-height:45px; border:1px solid #d5dcd5; border-radius:11px; background:white; font-size:.83rem; }
[data-testid="stSidebar"] [data-testid="stExpander"] { background:#fff; border:1px solid #e0e5de; border-radius:12px; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary p { font-size:.78rem; font-weight:600; }
[data-testid="stSidebar"] [data-testid="stButton"] button { min-height:39px; border-radius:9px; font-size:.76rem; }
.cl-brand { display:flex; align-items:center; gap:10px; color:#202520; font-size:19px; font-weight:800; letter-spacing:-.05em; text-decoration:none; }
.cl-brand em { font-style:normal; color:#23785f; }
.cl-logo { width:37px; height:37px; display:grid; place-items:center; border-radius:11px; background:#183f33; color:#bde9d2; flex-shrink:0; }
.cl-sidebar-label { color:#8a918b; font-size:10px; letter-spacing:.16em; font-weight:700; margin:10px 0 0; }
.cl-sidebar-note { font-size:11px; line-height:1.7; color:#879087; margin:7px 0 18px 48px; }
.cl-topbar { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:0 0 20px; border-bottom:1px solid #dfe3dc; margin-bottom:24px; }
.cl-topbar .cl-top-title { font-weight:600; font-size:12px; color:#6c766e; }
.cl-nav { display:flex; align-items:center; gap:24px; }
.cl-nav a { color:#626a63; font-size:12px; font-weight:600; text-decoration:none; }
.cl-nav a:first-child { color:#1e5b47; }
.cl-hero { position:relative; overflow:hidden; padding:32px 34px 29px; border-radius:21px; border:1px solid #dce7de; background:radial-gradient(circle at 94% 5%,#dcefe5 0,transparent 40%),linear-gradient(130deg,#f3f9f5,#fafcf9); margin-bottom:24px; }
.cl-eyebrow { display:inline-flex; align-items:center; gap:7px; border:1px solid #c5ddd1; border-radius:999px; color:#28735d; padding:6px 11px; font-size:10px; font-weight:700; letter-spacing:.02em; background:rgba(255,255,255,.75); }
.cl-dot { display:inline-block; width:6px; height:6px; border-radius:50%; background:#2c8c6c; flex-shrink:0; }
.cl-hero h1 { margin:17px 0 13px; padding:0; font-size:clamp(28px,3vw,42px); line-height:1.28; font-weight:800; letter-spacing:-.055em; color:#17231e; }
.cl-hero h1 span { color:#2a755e; }
.cl-hero>p { font-size:12px; line-height:1.9; margin:0; color:#667069; }
.cl-stats { display:flex; flex-wrap:wrap; gap:25px; padding-top:21px; margin-top:22px; border-top:1px solid #dce6de; }
.cl-stat { min-width:74px; }
.cl-stat strong { color:#1e5b47; font-size:20px; letter-spacing:-.04em; font-weight:800; }
.cl-stat small { display:block; color:#7c837e; font-size:10px; margin-top:5px; }
.cl-stat:last-child { margin-left:auto; align-self:center; text-align:right; }
.cl-stat:last-child strong { font-size:11px; font-weight:600; letter-spacing:0; }
.cl-workspace { display:flex; align-items:center; gap:13px; margin:2px 0 21px; scroll-margin-top:80px; }
.cl-company-icon { width:43px; height:43px; display:grid; place-items:center; color:#2a745c; background:#e8f1eb; border:1px solid #dce7de; border-radius:13px; flex-shrink:0; }
.cl-workspace h2 { padding:0; margin:0 0 4px; font-size:20px; line-height:1.3; letter-spacing:-.04em; font-weight:800; color:#202c24; }
.cl-workspace p { font-size:10px; line-height:1.7; color:#7b857d; margin:0; }
.cl-workspace .cl-badge { margin-left:auto; }
.cl-badge { display:inline-flex; align-items:center; gap:6px; padding:7px 10px; border:1px solid #d5e5da; border-radius:8px; color:#39735e; background:#eef5f1; font-size:10px; font-weight:700; white-space:nowrap; }
.st-key-question_workspace { background:#fff; border:1px solid #dbe1db; border-radius:18px; padding:27px 29px 29px; box-shadow:0 8px 30px rgba(31,52,43,.035); }
.st-key-question_workspace>div { gap:1rem; }
.st-key-question_workspace h3 { padding-top:0; margin-bottom:1px; color:#24352b; }
.st-key-question_workspace [data-testid="stCaptionContainer"] { font-size:.78rem; }
[class*="st-key-preset_"] button { min-height:68px!important; justify-content:flex-start!important; border:1px solid #dde2dc!important; border-radius:12px!important; background:#fff!important; color:#566259!important; padding:14px 18px!important; transition:background .15s,border-color .15s; }
[class*="st-key-preset_"] button [data-testid="stMarkdownContainer"] p { font-size:14px; font-weight:700; letter-spacing:-.02em; }
[class*="st-key-preset_"] button:hover { border-color:#93b4a2!important; background:#f7faf7!important; }
[class*="st-key-preset_"] button[kind="primary"] { border-color:#2a745c!important; background:#edf6f1!important; color:#1e5b47!important; box-shadow:inset 0 0 0 1px #2a745c; }
[class*="st-key-preset_"] button [data-testid="stIconMaterial"] { font-size:23px!important; color:#4b8068; margin-right:7px; }
[class*="st-key-follow_"] button { min-height:48px!important; justify-content:flex-start!important; text-align:left!important; background:white!important; border:1px solid #e0e6de!important; border-radius:9px!important; padding:10px 13px!important; color:#59635c!important; }
[class*="st-key-follow_"] button:hover { border-color:#abc9b7!important; color:#1e604a!important; background:#f9fcf8!important; }
[class*="st-key-follow_"] button p { font-size:12px!important; font-weight:500!important; line-height:1.8!important; white-space:normal!important; text-overflow:clip!important; text-align:left; }
.st-key-question_workspace [data-testid="stExpander"] { border:1px solid #e1e6df; border-radius:12px; background:#f7f9f5; }
.st-key-question_workspace [data-testid="stExpander"] summary p { color:#657367; font-size:.8rem; font-weight:600; }
.st-key-question_workspace [data-testid="stExpanderDetails"] { padding:1rem; }
[data-testid="stTextArea"] textarea { border-radius:11px; background:#fcfdfb; border-color:#cfd8cf; font-size:13px; line-height:1.85; padding:15px; }
[data-testid="stTextArea"] label p { color:#39493d; font-size:.85rem; font-weight:700; }
[data-testid="stCheckbox"] p { font-size:.77rem; color:#626e65; }
.st-key-preview button, .st-key-ask button { min-height:47px!important; border-radius:11px!important; font-size:.84rem; font-weight:700; }
.st-key-preview button { color:#475b4e; background:#fff; border-color:#bdcbbf; }
.st-key-ask button { background:#1e5b47!important; border-color:#1e5b47!important; color:white!important; }
.st-key-ask button:hover:not(:disabled) { background:#174936!important; }
.st-key-ask button:disabled { background:#bac7be!important; border-color:#bac7be!important; color:white!important; }
.st-key-company_snapshot { border:1px solid #dce1db; border-radius:14px; background:white; padding:15px; }
.st-key-company_snapshot [data-testid="stMarkdownContainer"] p { color:#626d65; font-size:12px; line-height:1.9; }
.st-key-company_snapshot [data-testid="stCaptionContainer"] p { font-size:10px; line-height:1.7; }
.st-key-company_snapshot [data-testid="stExpander"] { border:0; background:#f5f7f3; }
.st-key-company_snapshot [data-testid="stDownloadButton"] button { border:1px solid #d8ddd7; border-radius:9px; width:100%; font-size:11px; padding:10px; color:#4e6455; }
.cl-mini-label { color:#8a918b; font-size:9px; font-weight:700; letter-spacing:.12em; margin-bottom:7px; }
.cl-mini-heading { display:flex; align-items:center; gap:8px; margin:0 0 13px; }
.cl-mini-heading h3 { margin:0; padding:0; color:#2c3c30; font-size:16px; font-weight:800; }
[data-testid="stAlert"] { border-radius:11px; font-size:.8rem; }
[data-testid="stAlert"] p { font-size:.8rem; line-height:1.8; }
hr { border-color:#e2e7df!important; margin:1rem 0!important; }
.cl-guide { margin-top:26px; border:1px solid #e0e5de; padding:21px 24px; border-radius:14px; background:#f0f5ef; scroll-margin-top:70px; }
.cl-guide h3 { margin:0 0 15px; padding:0; color:#3d5746; font-size:13px; font-weight:700; }
.cl-guide-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:19px; }
.cl-guide-grid b { display:block; color:#3c6049; font-size:11px; margin-bottom:5px; }
.cl-guide-grid p { color:#7a857a; font-size:10px; line-height:1.8; margin:0; }
.cl-footer { display:flex; justify-content:space-between; gap:12px; border-top:1px solid #e0e6de; padding:19px 0 0; margin-top:26px; font-size:10px; color:#8b958c; }
@media (max-width:1000px) {
 [data-testid="stMainBlockContainer"] { padding:4.4rem 1.5rem 3rem; }
 .cl-hero { padding:25px; } .cl-hero h1 { font-size:33px; }
 .st-key-question_workspace { padding:22px; }
 [class*="st-key-preset_"] button { min-height:61px!important; padding:12px!important; }
}
@media (max-width:600px) {
 [data-testid="stMainBlockContainer"] { padding:4.4rem 1rem 2rem; }
 .cl-topbar { margin-bottom:16px; padding-bottom:15px; }
 .cl-topbar .cl-top-title { font-size:10px; } .cl-nav { gap:12px; } .cl-nav a { font-size:10px; }
 .cl-hero { padding:23px 21px; border-radius:16px; } .cl-hero h1 { font-size:29px; }
 .cl-hero>p { font-size:11px; } .cl-stats { gap:18px; } .cl-stat:last-child { display:none; }
 .cl-workspace { gap:10px; } .cl-workspace h2 { font-size:18px; } .cl-workspace .cl-badge { display:none; }
 .st-key-question_workspace { padding:20px 16px; }
 [class*="st-key-preset_"] button { min-height:55px!important; padding:9px!important; }
 [class*="st-key-preset_"] button p { font-size:12px!important; }
 [class*="st-key-preset_"] button [data-testid="stIconMaterial"] { font-size:18px!important; margin-right:2px; }
 .cl-guide-grid { grid-template-columns:1fr; gap:14px; }
 .cl-footer { flex-direction:column; }
}
</style>'''


def apply_theme() -> None:
    st.html(CSS)


def sidebar_brand() -> None:
    st.html(f'''<div class="cl-brand"><span class="cl-logo">{APERTURE}</span><span>CareerLens <em>AI</em></span></div>
<p class="cl-sidebar-note">공시로 알아보는 나의 다음 회사</p><p class="cl-sidebar-label">분석 대상</p>''')


def render_hero(company_count: int, year: int, key_configured: bool) -> None:
    status = "OpenAI 키 설정됨" if key_configured else "원문 먼저 확인할 수 있어요"
    st.html(f'''<div class="cl-topbar"><span class="cl-top-title">기업을 이해하는 새로운 시선</span><nav class="cl-nav" aria-label="페이지 메뉴"><a href="#workspace">기업 탐색</a><a href="#guide">이용 가이드</a></nav></div>
<section class="cl-hero"><span class="cl-eyebrow"><span class="cl-dot"></span>DART 공시 기반 커리어 리서치</span>
<h1>회사에 대한 궁금증,<br><span>공시에서 답을 찾으세요.</span></h1>
<p>복잡한 사업보고서를 취업준비생의 눈높이로 읽어보세요.<br>실제 사업부터 성장·급여·근무환경까지, 확인된 근거와 함께 설명합니다.</p>
<div class="cl-stats"><div class="cl-stat"><strong>{company_count}개</strong><small>분석 기업</small></div>
<div class="cl-stat"><strong>{year}</strong><small>선택 사업연도</small></div><div class="cl-stat"><strong>18개</strong><small>추천 질문</small></div>
<div class="cl-stat"><strong><span class="cl-dot"></span> {status}</strong><small>원문과 함께 확인하세요</small></div></div></section>''')


def workspace_heading(company: str, year: int, has_dart: bool) -> None:
    company = escape(company)
    sources = "업로드한 공시 · 저장된 DART 직원 현황" if has_dart else "업로드한 공시"
    st.html(f'''<div id="workspace" class="cl-workspace"><span class="cl-company-icon" aria-hidden="true">{BUILDING}</span>
<div><h2>{company}</h2><p>{year} 사업연도 · {sources}</p></div><span class="cl-badge"><span class="cl-dot"></span>공시 근거 우선</span></div>''')


def company_snapshot(sections, company: str, year: int, dart_snapshot, root: Path, read_pdf) -> None:
    with st.container(border=True, key="company_snapshot"):
        st.html(f'<div class="cl-mini-label">COMPANY SNAPSHOT</div><div class="cl-mini-heading"><h3>{escape(company)}</h3></div>')
        overview = next((s for s in sections if s.category == "business"), None)
        if overview:
            preview = overview.text
            if preview.startswith(overview.section):
                preview = preview[len(overview.section):].lstrip()
            compact = re.sub(r"\s+", " ", preview).strip()
            st.caption("사업의 개요 · 원문 발췌")
            st.write(compact[:210] + ("…" if len(compact) > 210 else ""))
            with st.expander("회사 소개 원문 더 보기"):
                st.write(preview[:1600] + ("…" if len(preview) > 1600 else ""))
                start, end = overview.start_pdf_page, overview.end_pdf_page
                st.caption(f"추출 절 범위: PDF {start}쪽" if start == end else f"추출 절 범위: PDF {start}–{end}쪽")
        else:
            st.caption("사업의 개요 항목이 없습니다.")
        if dart_snapshot is not None:
            st.caption("✓ DART 직원 현황 연결됨")
        if sections:
            pdf_name = Path(sections[0].source_file.replace("\\", "/")).name
            path = root / "reports" / pdf_name
            if path.is_file():
                st.download_button("사업보고서 PDF 내려받기", data=read_pdf(str(path)), file_name=pdf_name,
                                   mime="application/pdf", key=f"pdf_{company}_{year}", width="stretch", icon=":material/download:")


def render_guide() -> None:
    st.html('''<section id="guide" class="cl-guide"><h3>처음이라면, 이렇게 살펴보세요.</h3><div class="cl-guide-grid">
<div><b>01. 회사와 질문 고르기</b><p>왼쪽에서 기업·사업연도를 선택하고, 여섯 카테고리에서 궁금한 질문을 골라보세요.</p></div>
<div><b>02. 원문부터 확인하기</b><p>‘원문 먼저 확인’으로 공시 자료를 살펴보세요. 모델 호출 없이 근거를 확인할 수 있어요.</p></div>
<div><b>03. 근거와 함께 읽기</b><p>답변 아래에서 문서·공시 링크와 연결된 논문을 확인하고, 실제 채용 조건은 공고와 대조하세요.</p></div>
</div></section><footer class="cl-footer"><span>CareerLens AI · 공시를 읽는 새로운 시선</span><span>공시 해설은 지원 판단을 돕는 참고 정보입니다.</span></footer>''')
