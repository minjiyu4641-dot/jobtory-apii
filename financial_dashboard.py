"""구직자를 위한 기업 재무 인사이트: OpenDART 실수집 → 분석 → UI (v3.1).

화면 구성
  검색 → 기업 헤더(기준·자료 확보 연도) → 핵심 실적 4개 → 점검 요약(위험 조건 6개 + 확인해 볼 점)
  → 탭: 실적·현금흐름 / 재무 조건 점검 / 5개년 지표 / 업종 비교 / 자료 출처
계산은 analysis/*, 문구·상태 정책은 app_modules/presentation.py에 있고 이 파일은 배치만 담당한다.
"""
from __future__ import annotations
import html
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from streamlit_searchbox import st_searchbox

from dart.client import DartAPIError, mask_secret
from dart.search import (load_catalog, find_companies, update_full_catalog, catalog_is_current, SEEDS,
                         HOLDING_COMPANY_KSIC)
from dart.pipeline import download_company_five_years, download_separate_for_comparison
from app_modules.engine import analyze, save_analysis_result
from app_modules.industry_auto import prepare_industry_benchmark
from app_modules.presentation import (
    METRIC_HELP, RULE_HELP, GROUPS, GROUP_INTRO, CASH_METRICS, SERVICE_BENCHMARK_METRICS,
    BENCHMARK_DIRECTION_TEXT, RULE_STATUS, chart_range, money, pct, delta_display,
    profit_transition, rule_state, rules_summary, flag_evidence, quartile_position,
    trend_sentence, build_checkpoints, safe_group, split_reason, GROWTH_BASE_FIELD, growth_change)
from analysis.financial_rules import RULES
from analysis.industry_comparison import METRIC_DIRECTION
from benchmarks.bok_client import DATA_DIR

ROOT = Path(__file__).resolve().parent
FONT = 'Pretendard, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif'
C = {  # 디자인 토큰 (CSS와 Plotly가 같은 값을 쓴다)
    'ink':'#1C2533', 'muted':'#5F6B7A', 'line':'#E3E7ED', 'paper':'#F4F7F5',
    'brand':'#2A745C', 'brand_soft':'#E0EFE7', 'second':'#8FA3BF', 'third':'#C7A46B',
    'neg':'#C2622D', 'grid':'#EDF0F4',
    # 적자 막대: 같은 주황 계열에서 진하기로 지표를 구분 (영업이익=진함, 당기순이익=연함)
    'neg_dark':'#A94B17', 'neg_light':'#EDAA80',
}
FIELD_LABEL = {
    'total_assets':'자산총계','total_liabilities':'부채총계','total_equity':'자본총계',
    'current_assets':'유동자산','current_liabilities':'유동부채','property_plant_equipment':'유형자산',
    'revenue':'매출액','operating_income':'영업이익','net_income':'당기순이익',
    'operating_cash_flow':'영업활동현금흐름','investing_cash_flow':'투자활동현금흐름',
    'financing_cash_flow':'재무활동현금흐름',
    'issued_capital':'자본금','equity_attributable_to_owners':'지배기업 소유주지분',
    'noncontrolling_interests':'비지배지분',
}
FINANCIAL_SECTOR_PREFIX = ('64', '65', '66')  # KSIC 금융·보험업



def render_dashboard(data: dict, meta: dict, report: dict, *,
                     on_ask=None, show_company_header: bool = True) -> None:
    """Render saved analysis; optional buttons prepare a question without an API call."""
    def ask(key, question, parent='performance'):
        if on_ask is not None:
            st.button('물어보기', key=key, on_click=on_ask,
                      args=(question, None, parent),
                      help='오른쪽 질문창에 입력합니다. 답변 받기를 누르면 AI가 설명합니다.')

    st.markdown(f'''<style>
    @import url("https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css");
    :root {{--ink:{C['ink']};--muted:{C['muted']};--line:{C['line']};--paper:{C['paper']};--brand:{C['brand']};--brand-soft:{C['brand_soft']};--card-line:rgba(49,51,63,.2);}}
    html, body, .stApp, button, input, textarea {{font-family:{FONT};}}



    #MainMenu, footer {{visibility:hidden}}
    [data-testid="stHeader"] {{background:transparent}}
    p, li {{line-height:1.6}}
    .num {{font-variant-numeric:tabular-nums}}
    /* 간격 규칙 (v3.4): 8 / 16 / 24 / 32px 네 단계만 쓴다.
       Streamlit은 요소 사이에 16px(1rem)을 자동으로 둔다. 큰 구획 사이는 여기에 16px을 더해 32px로 맞춘다.
       - 큰 구획(제목→검색창, 검색창→회사 카드, 회사 카드→섹션, 점검 요약→탭, 섹션→섹션): 32px
       - 섹션 제목→설명: 4px, 설명→내용: 16px / 카드 안 요소 사이: 8~16px
       주의: Streamlit 마크다운 블록은 첫/마지막 요소의 바깥 여백(margin)을 없애므로 구획 간격은 padding으로 준다. */
    .gap-after {{padding-bottom:16px}}
    /* Streamlit은 마크다운 블록에 -16px 아래 여백을 줘 문단 여백을 상쇄한다. 이 앱이 HTML로 그리는 블록(div로 시작)에서는
       이 상쇄 때문에 요소 사이 기본 16px이 사라지므로 0으로 되돌려 다른 요소와 같은 간격 규칙을 따르게 한다. */
    [data-testid="stMarkdownContainer"]:has(> div) {{margin-bottom:0}}
    /* 상단 */
    .brand {{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;padding-bottom:16px}}
    .brand-name {{font-size:1.35rem;font-weight:800;letter-spacing:-.03em;color:var(--brand)}}
    .brand-desc {{color:var(--muted);font-size:.92rem}}
    /* 빈 화면 */
    .intro {{background:#fff;border:1px solid var(--line);border-radius:14px;padding:28px 30px;margin-top:18px}}
    .intro h2 {{font-size:1.5rem;letter-spacing:-.03em;margin:0 0 6px}}
    .intro-grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px;margin-top:18px}}
    .intro-item b {{display:block;font-size:1rem;margin-bottom:4px}}
    .intro-item span {{color:var(--muted);font-size:.9rem;line-height:1.55}}
    .intro-note {{margin-top:20px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}}
    /* 기업 헤더 */
    .company {{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px;margin:8px 0 0}}  /* 검색 줄 아래 여백 8px + 자동 16px + 8px = 32px */
    .company-name {{font-size:1.9rem;font-weight:800;letter-spacing:-.04em;line-height:1.2}}
    .chips {{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}}
    .chip {{font-size:.8rem;padding:3px 10px;border-radius:999px;background:var(--paper);color:var(--muted);border:1px solid var(--line);white-space:nowrap}}
    .chip.strong {{background:var(--brand-soft);color:var(--brand);border-color:transparent;font-weight:650}}
    .years-row {{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px}}
    .years-label {{font-size:.82rem;color:var(--muted);font-weight:600}}
    .years {{display:flex;gap:5px}}
    .yr {{width:46px;text-align:center;font-size:.74rem;padding:6px 0;border-radius:6px;border:1.5px solid var(--brand);color:#fff;background:var(--brand);font-variant-numeric:tabular-nums}}
    .yr.off {{background:#fff;color:var(--muted);border:1.5px dashed #B6BFCB}}
    /* 섹션 */
    .sec {{font-size:1.12rem;font-weight:750;letter-spacing:-.02em;padding-top:16px;margin:0 0 4px}}
    .sec-sub {{color:var(--muted);font-size:.86rem;margin-bottom:0}}
    /* KPI */
    .kpi {{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px;height:132px;display:flex;flex-direction:column;justify-content:space-between}}
    .kpi-label {{font-size:.86rem;color:var(--muted);font-weight:600}}
    .kpi-value {{font-size:clamp(1.2rem,2vw,1.6rem);font-weight:800;letter-spacing:-.03em;white-space:nowrap;font-variant-numeric:tabular-nums}}
    .kpi-value.neg {{color:{C['neg']}}}
    .kpi-foot {{display:flex;gap:8px;align-items:center;font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums;flex-wrap:wrap}}
    .tag {{font-size:.74rem;font-weight:700;padding:2px 8px;border-radius:5px;background:#F1E6DD;color:#8A4116}}
    .tag.info {{background:var(--brand-soft);color:var(--brand)}}
    /* 점검 요약 */
    .summary {{background:#fff;border:1px solid var(--line);border-left:5px solid var(--brand);border-radius:12px;padding:24px}}
    .summary-head {{font-size:1.32rem;font-weight:800;letter-spacing:-.03em;line-height:1.35}}
    .summary-sub {{color:var(--muted);font-size:.88rem;margin-top:6px}}
    .strip {{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin:16px 0 4px}}
    .cell {{border-radius:8px;padding:9px 10px;font-size:.78rem;line-height:1.35;min-height:62px}}
    .cell b {{display:block;font-size:.74rem;margin-bottom:3px;font-weight:700}}
    .st-risk {{background:#FBE4E2;color:#8E1B16}}
    .st-watch {{background:#FCEFD8;color:#7A4606}}
    .st-clear {{background:#EEF1F5;color:#44546A}}
    .st-unknown {{background:#fff;color:#7A8494;border:1.5px dashed #C4CBD5}}
    .checks {{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}}
    .check {{display:grid;grid-template-columns:22px 1fr;gap:8px;padding:8px 0}}
    .check-mark {{width:18px;height:18px;border-radius:50%;margin-top:3px}}
    .check-mark.attention {{background:{C['neg']}}}
    .check-mark.info {{background:var(--brand)}}
    .check-mark.data {{background:#fff;border:2px dashed #9AA4B2}}
    .check-title {{font-weight:700;font-size:.95rem}}
    .check-detail {{color:var(--muted);font-size:.86rem;line-height:1.55}}
    /* 규칙 표 */
    .rule {{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;display:grid;grid-template-columns:minmax(220px,1.4fr) 2fr;gap:18px;align-items:center}}
    .rule-name {{font-weight:700;font-size:.97rem}}
    .rule-explain {{font-size:.84rem;color:var(--muted);margin-top:4px;line-height:1.5}}
    .rule-evidence {{font-size:.82rem;margin-top:8px;font-variant-numeric:tabular-nums;display:flex;gap:8px;align-items:flex-start}}
    .ev-tag {{flex:none;font-size:.74rem;font-weight:700;padding:1px 8px;border-radius:5px;background:var(--brand-soft);color:var(--brand)}}
    .rule-years {{display:grid;grid-template-columns:repeat(5,1fr);gap:5px}}
    .ry {{border-radius:6px;padding:6px 4px;text-align:center;font-size:.72rem;line-height:1.3}}
    .ry span {{display:block;font-size:.7rem;opacity:.8;font-variant-numeric:tabular-nums}}
    @media (max-width:760px) {{ .rule {{grid-template-columns:1fr}} .strip {{grid-template-columns:repeat(3,1fr)}} }}
    /* 지표 카드 */
    .m-title {{font-weight:750;font-size:1rem}}
    .m-value {{font-size:1.45rem;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums;margin-top:2px}}
    .m-trend {{color:var(--muted);font-size:.84rem}}
    .m-meaning {{color:var(--muted);font-size:.84rem;padding:4px 0 8px;line-height:1.5}}
    .m-delta {{font-size:.95rem;font-weight:700;color:var(--muted);margin-left:4px}}
    /* 띠(band)로 나눠 그린 카드: 같은 줄 좌우 칸의 높이가 띠마다 맞춰진다. 테두리를 이어 하나의 카드처럼 보이게 함 */
    [class*="st-key-cardtop_"], [class*="st-key-cardmid_"], [class*="st-key-cardbot_"] {{
      background:#fff;border:1px solid var(--card-line);border-top:0;border-bottom:0;padding:8px 16px 0}}
    [class*="st-key-cardtop_"] {{border-top:1px solid var(--card-line);border-radius:12px 12px 0 0;padding-top:16px}}
    [class*="st-key-cardbot_"] {{border-bottom:1px solid var(--card-line);border-radius:0 0 12px 12px;padding-bottom:16px}}
    /* 계산 방법과 읽을 때 주의할 점: 위 설명 문구와 같은 글자 크기 */
    .how {{font-size:.84rem;line-height:1.6;color:var(--ink)}}
    .how p {{margin:0 0 8px}}
    /* 값이 비어 있는 연도: [연도] [계정 또는 변화 유형] 사유 */
    .miss-title {{font-weight:700;font-size:.84rem;margin:16px 0 4px}}
    .miss-row {{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0;font-size:.84rem}}
    .tag-year {{font-size:.76rem;font-weight:650;padding:2px 8px;border-radius:6px;background:var(--paper);border:1px solid var(--line);font-variant-numeric:tabular-nums}}
    .tag-acct {{font-size:.76rem;font-weight:650;padding:2px 8px;border-radius:6px;background:var(--brand-soft);color:var(--brand)}}
    .miss-text {{color:var(--muted)}}
    /* 표 (흰 표 + 머리글에만 색) */
    .tbl {{width:100%;border-collapse:separate;border-spacing:0;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:.86rem}}
    .tbl th {{background:#EEF2F7;color:var(--ink);font-weight:650;padding:9px 12px;text-align:center;border-bottom:1px solid var(--line);white-space:nowrap}}
    .tbl tbody th {{text-align:left;border-right:1px solid var(--line)}}
    .tbl td {{padding:8px 12px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;white-space:nowrap}}
    .tbl tbody tr:last-child td, .tbl tbody tr:last-child th {{border-bottom:0}}
    .tbl .l {{text-align:left}} .tbl .c {{text-align:center}} .tbl .r {{text-align:right}}
    .tbl a {{color:var(--brand);text-decoration:none;font-weight:600}}
    .tbl-wrap {{overflow-x:auto}}
    /* 업종 내 위치 안내 문장: 옅은 파란 상자 + 왼쪽 색 막대, 구간 표현만 굵게 */
    .pos {{font-size:.84rem;line-height:1.5;padding:8px 12px;border-radius:8px;border-left:4px solid var(--brand);background:#EEF3FA;color:var(--ink);margin-top:8px}}
    .pos b {{color:var(--brand);font-weight:750}}
    .small {{color:var(--muted);font-size:.82rem;line-height:1.55}}
    .unit {{white-space:nowrap}}
    /* 차트 범례(차트 가로 가운데) */
    .legend {{display:flex;justify-content:center;gap:18px;flex-wrap:wrap;font-size:.8rem;color:var(--ink);margin:4px 0 0}}
    .legend span {{display:inline-flex;align-items:center;gap:5px}}
    .legend i {{display:inline-block;width:11px;height:11px;border-radius:2px}}
    /* 업종 비교 수치 (가운데 정렬) */
    .bench {{display:flex;gap:32px;flex-wrap:wrap;justify-content:center;padding-bottom:8px}}
    .bench-item {{display:flex;flex-direction:column;align-items:center;gap:2px}}
    .bench-label {{font-size:.78rem;color:var(--muted);display:flex;align-items:center;gap:6px}}
    .bench-value {{font-size:1.25rem;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
    .bench-value.me {{color:var(--brand)}}
    .bench-label i {{display:inline-block;flex:none}}
    .sym-dot {{width:11px;height:11px;border-radius:50%;background:var(--brand)}}
    .sym-med {{width:3px;height:13px;background:#44546A}}
    .bench-band {{display:flex;justify-content:center;align-items:center;gap:6px;font-size:.78rem;color:var(--muted);padding-bottom:8px}}
    .bench-band i {{display:inline-block;flex:none}}
    .sym-band {{width:18px;height:11px;background:#C9D6EA;border-radius:2px}}
    /* Streamlit 기본 요소 */
    [data-testid="stPlotlyChart"] {{overflow:hidden}}
    [data-testid="stExpander"] {{background:#fff;border:1px solid var(--line);border-radius:10px}}
    /* 표를 담은 펼침 영역(key가 tbl_로 시작)은 페이지 배경색, 표만 흰색 */
    [class*="st-key-tbl_"] [data-testid="stExpander"], [class*="st-key-tbl_"][data-testid="stExpander"],
    [class*="st-key-tbl_"] details {{background:var(--paper)}}
    [data-baseweb="tab-list"] {{gap:2px;border-bottom:1px solid var(--line)}}
    button[data-baseweb="tab"] {{padding:.65rem 1rem;font-weight:600}}
    button[data-baseweb="tab"][aria-selected="true"] {{color:var(--brand)}}
    [data-testid="stVerticalBlockBorderWrapper"] {{background:#fff;border-radius:12px}}
    </style>''', unsafe_allow_html=True)



    # ───────────────────────── 공통 도우미 ─────────────────────────
    def esc(s) -> str:
        return html.escape(str(s if s is not None else ''))


    def block(markup: str) -> None:
        """들여쓰기가 마크다운 코드블록으로 해석되지 않도록 한 줄로 합쳐 출력."""
        st.markdown(''.join(line.strip() for line in markup.splitlines()), unsafe_allow_html=True)


    def with_unit(text: str | None, unit: str | None) -> str:
        """설명 문구 뒤에 '(단위 : …)'를 붙인다. 문구가 없으면 단위만 쓴다. (HTML 반환)"""
        parts = [esc(text)] if text else []
        if unit:
            parts.append(f'<span class="unit">(단위 : {esc(unit)})</span>')
        return ' '.join(parts)


    def section(title: str, sub: str | None = None, unit: str | None = None) -> None:
        body = with_unit(sub, unit)
        block(f'<div class="sec">{esc(title)}</div>' + (f'<div class="sec-sub">{body}</div>' if body else ''))


    def legend_html(items) -> str:
        """차트 가로 가운데 범례. items: [(이름, [색, ...])] — 색이 하나도 없는 항목은 빼고 그린다."""
        out = ''
        for name, colors in items:
            if not colors:
                continue
            swatches = ''.join(f'<i style="background:{c}"></i>' for c in colors)
            out += f'<span>{swatches}{esc(name)}</span>'
        return f'<div class="legend">{out}</div>' if out else ''


    def card_bands(pair, renderers) -> None:
        """같은 줄 카드 2개를 '띠(band)' 단위로 나란히 그린다. 띠마다 한 줄(columns)이라 높이가 줄 단위로 맞춰져,
        문구가 1줄이든 3줄이든 다음 항목(차트·설명·펼침 칸)이 좌우 같은 선에서 시작한다.

        pair: [(고유 id, 데이터), ...] 최대 2개. renderers: 띠별 그리기 함수(데이터를 받음), 위→아래 순서.
        띠 칸은 key 앞부분(cardtop_/cardmid_/cardbot_)으로 CSS가 테두리를 이어 하나의 카드처럼 보이게 한다.
        """
        last = len(renderers) - 1
        with st.container(gap=None):
            for i, render in enumerate(renderers):
                part = 'top' if i == 0 else 'bot' if i == last else 'mid'
                for col, (uid, payload) in zip(st.columns(2), pair):
                    with col, st.container(key=f'card{part}_{uid}_{i}', height='stretch'):
                        render(payload)


    def growth_changes(metric: str) -> dict:
        """성장률을 계산하지 않은 연도(전기 0 이하)의 변화 유형·금액 변화. {연도: growth_change 결과}"""
        field = GROWTH_BASE_FIELD.get(metric)
        if not field:
            return {}
        out = {}
        for prev_k, k in zip(keys, keys[1:]):
            if k not in present or prev_k not in present:
                continue
            item = report['ratios']['years'].get(k, {}).get('ratios', {}).get(metric)
            change = growth_change(item, data['financials'][prev_k]['statement'].get(field),
                                   data['financials'][k]['statement'].get(field))
            if change:
                out[k] = change
        return out


    def shown_colors(values, pos_color, neg_color) -> list[str]:
        """그래프에 실제로 칠해진 색만: 0 이상 막대가 있으면 기본색, 음수 막대가 있으면 주황 계열."""
        vals = [v for v in values if v is not None]
        return ([pos_color] if any(v >= 0 for v in vals) else []) + ([neg_color] if any(v < 0 for v in vals) else [])


    def html_table(header, rows, *, align, row_header=False) -> str:
        """흰 표 + 머리글(첫 행, row_header면 첫 열도)에만 색. align: 열별 'l'/'c'/'r'. 셀 값은 이미 HTML 이스케이프된 문자열."""
        head = ''.join(f'<th>{h}</th>' for h in header)
        body = ''
        for row in rows:
            cells = ''
            for i, (value, a) in enumerate(zip(row, align)):
                cells += (f'<th class="{a}">{value}</th>' if (row_header and i == 0) else f'<td class="{a}">{value}</td>')
            body += f'<tr>{cells}</tr>'
        return f'<div class="tbl-wrap"><table class="tbl"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


    def fmt_metric(metric: str, v) -> str:
        return money(v) if metric in CASH_METRICS else pct(v)


    PLOT_CONFIG = {'displayModeBar': False, 'responsive': True}


    def stretch(fn) -> dict:
        """Streamlit 버전에 따라 width='stretch' 또는 구버전 use_container_width=True를 쓴다."""
        import inspect
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        return {'width': 'stretch'} if 'width' in params else {'use_container_width': True}


    def plot_base(fig, *, height=300):
        """단위는 설명 문구 뒤 '(단위 : …)'로 보여 주므로 세로축 제목은 쓰지 않는다. 범례도 HTML로 따로 그린다."""
        fig.update_layout(
            height=height, margin=dict(l=8, r=12, t=14, b=10),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            font=dict(family=FONT, color=C['ink'], size=12),
            showlegend=False, hovermode='x unified', bargap=.35, bargroupgap=.08,
            hoverlabel=dict(font=dict(family=FONT)),
            xaxis=dict(type='category', showgrid=False, tickfont=dict(size=12)),
            yaxis=dict(gridcolor=C['grid'], zeroline=True, zerolinecolor='#7D8796', zerolinewidth=1.4,
                       tickformat=',.0f'))
        return fig


    def label_positions(values) -> list[str]:
        """꺾은선 점 숫자 위치를 선이 지나지 않는 쪽으로 정한다.

        골짜기(양옆보다 낮음)는 아래, 봉우리는 위, 오르막 중간은 왼쪽 위, 내리막 중간은 오른쪽 위.
        """
        out = []
        n = len(values)
        for i, v in enumerate(values):
            prev = values[i - 1] if i > 0 else None
            nxt = values[i + 1] if i + 1 < n else None
            if v is None:
                out.append('top center')
                continue
            lower_than = [x for x in (prev, nxt) if x is not None]
            if lower_than and all(v < x for x in lower_than):
                out.append('bottom center')
            elif prev is not None and nxt is not None and prev < v < nxt:
                out.append('top left')
            elif prev is not None and nxt is not None and prev > v > nxt:
                out.append('top right')
            else:
                out.append('top center')
        return out


    def mark_missing(fig, keys, present):
        """자료가 없는 연도는 빈칸 대신 '자료 없음'을 적어 0으로 오해하지 않게 한다.

        가로축은 5개 연도로 고정한다. 예전에는 주석 위치를 '2021' 같은 문자열로 주어 Plotly가 이를 2021번째 칸으로
        읽었고, 축이 크게 늘어나 실제 점·막대가 한쪽에 몰렸다. 주석은 칸 번호(0~4)로 지정한다.
        """
        fig.update_xaxes(type='category', categoryorder='array', categoryarray=list(keys),
                         range=[-0.5, len(keys) - 0.5])
        for i, k in enumerate(keys):
            if k not in present:
                fig.add_annotation(x=i, y=0, text='자료 없음', showarrow=False, yshift=12,
                                   font=dict(size=11, color=C['muted']))
        return fig


    def series(data, keys, field):
        vals = [data['financials'].get(k, {}).get('statement', {}).get(field) for k in keys]
        return [v / 1e8 if v is not None else None for v in vals]


    def bars(keys, values, name, color, neg_color=None):
        """음수 막대는 주황 계열로 칠해 0선 아래임을 한 번 더 알린다. 지표마다 주황의 진하기를 달리할 수 있다."""
        colors = [(neg_color or C['neg']) if (v is not None and v < 0) else color for v in values]
        return go.Bar(x=keys, y=values, name=name, marker_color=colors,
                      hovertemplate='%{y:,.1f}억 원<extra>' + name + '</extra>')


    BAND = '#C9D6EA'   # 업종 중간 50% 구간 띠
    MEDIAN = '#44546A' # 업종 중앙값 선


    def quartile_figure(item):
        """p25~p75 구간(있을 때만)을 띠로, 중앙값을 굵은 세로선으로, 기업을 점으로 표시.

        숫자는 그래프 안에 쓰지 않는다(값이 가까우면 글자가 겹침). 정확한 값은 그래프 아래 수치 줄(bench_html)에 둔다.
        """
        company = item.get('value'); q = item.get('quantiles') or {}; median = item.get('median')
        p25, p75 = q.get('p25'), q.get('p75')
        left, right = chart_range([company, median, p25, p75])
        fig = go.Figure()
        fig.add_shape(type='line', x0=left, x1=right, y0=0, y1=0, line=dict(color='#D3D9E1', width=2), layer='below')
        if p25 is not None and p75 is not None:
            fig.add_shape(type='rect', x0=p25, x1=p75, y0=-.34, y1=.34, line=dict(width=0),
                          fillcolor=BAND, layer='below')
            fig.add_trace(go.Scatter(x=[p25, p75], y=[0, 0], mode='markers', marker=dict(size=1, opacity=0),
                                     customdata=['25%', '75%'], showlegend=False,
                                     hovertemplate='업종 %{customdata} 분위 %{x:,.1f}%<extra></extra>'))
        if median is not None:
            fig.add_shape(type='line', x0=median, x1=median, y0=-.5, y1=.5, line=dict(color=MEDIAN, width=3.5))
            fig.add_trace(go.Scatter(x=[median], y=[0], mode='markers', marker=dict(size=1, opacity=0), showlegend=False,
                                     hovertemplate=f'업종 중앙값 {median:,.1f}%<extra></extra>'))
        if company is not None:
            fig.add_trace(go.Scatter(x=[company], y=[0], mode='markers',
                                     marker=dict(color=C['brand'], size=20, line=dict(color='white', width=3)),
                                     hovertemplate=f'이 기업 {company:,.1f}%<extra></extra>', showlegend=False))
        fig.update_layout(height=110, margin=dict(l=12, r=12, t=8, b=28), paper_bgcolor='rgba(0,0,0,0)',
                          plot_bgcolor='rgba(0,0,0,0)', font=dict(family=FONT, size=11),
                          xaxis=dict(range=[left, right], ticksuffix='%', showgrid=False, zeroline=False,
                                     tickformat=',.0f', tickfont=dict(color=C['muted'], size=11)),
                          yaxis=dict(visible=False, range=[-.75, .75]), hovermode='closest')
        return fig


    def bench_html(item) -> str:
        """그래프 아래 가운데 수치 줄: 이 기업 / 업종 중앙값 + 띠 범례. 기호는 그래프 표시(점·세로선·띠)와 같다.

        띠(업종 가운데 50%)는 숫자 없이 범례 기호만 보여 준다. 분위값이 공표되지 않아 띠를 그리지 않으면 범례도 뺀다.
        """
        company = item.get('value'); median = item.get('median'); q = item.get('quantiles') or {}
        band = ('<div class="bench-band"><i class="sym-band"></i>업종 보통 범위 (순위 25~75%)</div>'
                if q.get('p25') is not None and q.get('p75') is not None else '')
        return ('<div class="bench">'
                f'<div class="bench-item"><span class="bench-label"><i class="sym-dot"></i>이 기업</span>'
                f'<span class="bench-value me">{esc(pct(company))}</span></div>'
                f'<div class="bench-item"><span class="bench-label"><i class="sym-med"></i>업종 중앙값</span>'
                f'<span class="bench-value">{esc(pct(median))}</span></div></div>' + band)



    # ───────────────────────── 공통 값 ─────────────────────────
    keys = [str(y if meta['settlement_month'] == 12 else y - 1) for y in meta['business_year_window']]
    latest = max(data['financials'], key=lambda k: data['financials'][k]['period_end'])
    annual = data['financials'][latest]
    stmt = annual['statement']
    ratios = report['ratios']['years'][latest]['ratios']
    prior = keys[keys.index(latest) - 1] if latest in keys and keys.index(latest) > 0 else None
    prev_stmt = data['financials'].get(prior, {}).get('statement', {}) if prior else {}
    prev_ratios = report['ratios']['years'].get(prior, {}).get('ratios', {}) if prior else {}
    fs_label = '연결재무제표' if data['fs_div'] == 'CFS' else '별도재무제표'
    industry_info = report.get('industry_auto') or {}
    induty = str((meta.get('company_info') or {}).get('induty_code') or '')
    comp = report.get('comparison') or {}
    industry_name = next((m.get('industry_name') for m in (comp.get('metrics') or {}).values() if m.get('industry_name')), None)

    # ───────────────────────── 기업 헤더 ─────────────────────────
    chips = [f'<span class="chip strong">{esc(fs_label)}</span>',
             f'<span class="chip">{esc(meta.get("stock_code") or "비상장")}</span>',
             f'<span class="chip">최근 결산 {esc(annual["period_end"])}</span>',
             '<span class="chip">사업보고서 기준</span>']
    if industry_name:
        chips.append(f'<span class="chip">업종 {esc(industry_name)}</span>')
    years_html = ''.join(f'<div class="yr{"" if k in data["financials"] else " off"}" title="{"자료 있음" if k in data["financials"] else "자료 없음"}">{esc(k)}</div>' for k in keys)
    company_name = (f'<div class="company-name">{esc(data["corp_name"])}</div>'
                    if show_company_header else '')
    block(f'''<div class="{"company" if show_company_header else "financial-basis"}">
    {company_name}
    <div class="chips">{"".join(chips)}</div>
    <div class="years-row"><span class="years-label">확보한 사업연도</span>
    <div class="years">{years_html}</div></div></div>''')

    if induty.startswith(FINANCIAL_SECTOR_PREFIX):
        st.warning('금융·보험업 회사는 재무제표 구조가 일반 기업과 달라 유동비율·영업이익률 같은 지표가 비어 있거나 의미가 다를 수 있어요.')

    # ───────────────────────── 핵심 실적 ─────────────────────────
    section(f'{latest}년 핵심 실적', '▲▼는 전년 대비 증감 방향만 나타내요. 좋고 나쁨을 색으로 구분하지 않았습니다.')
    kpis = [('매출액', stmt.get('revenue'), prev_stmt.get('revenue'), 'money'),
            ('영업이익', stmt.get('operating_income'), prev_stmt.get('operating_income'), 'money'),
            ('당기순이익', stmt.get('net_income'), prev_stmt.get('net_income'), 'money'),
            ('영업이익률', ratios.get('operating_margin', {}).get('value'),
             prev_ratios.get('operating_margin', {}).get('value'), 'ratio')]
    for i, (col, (label, cur, prev, unit)) in enumerate(zip(st.columns(4), kpis)):
        shown = pct(cur) if unit == 'ratio' else money(cur)
        neg = ' neg' if cur is not None and cur < 0 else ''
        d = delta_display(cur, prev, unit)
        transition = profit_transition(cur, prev) if label in ('영업이익', '당기순이익') else None
        foot = f'{esc(d["arrow"])} {esc(d["label"])}'
        if transition:
            foot = f'<span class="tag{" info" if transition == "흑자 전환" else ""}">{esc(transition)}</span>' + (
                '' if d['label'].startswith('전년 값') else foot)
        with col:
            block(f'<div class="kpi"><div class="kpi-label">{esc(label)}</div>'
                  f'<div class="kpi-value{neg}">{esc(shown)}</div><div class="kpi-foot">{foot}</div></div>')
            ask(f'ask_financial_kpi_{i}', f'{latest}년 {label}의 수치와 전년 대비 변화, 공시에서 확인할 점을 설명해줘.')

    # ───────────────────────── 점검 요약 ─────────────────────────
    summary = rules_summary(report['rules'], latest, RULES)
    if summary['flagged']:
        head = f'{latest}년 기준, 눈여겨볼 재무 조건 {summary["total"]}개 중 {summary["flagged"]}개에 해당해요'
    else:
        head = f'{latest}년 기준, 점검한 재무 조건 {summary["total"] - summary["unknown"]}개에는 해당하지 않아요'
    sub = '해당하지 않는다는 것은 이 몇 가지 조건이 나타나지 않았다는 뜻일 뿐, 회사가 안전하다는 판정이 아니에요.'
    if summary['unknown']:
        sub = f'{summary["unknown"]}개 조건은 자료가 부족해 확인하지 못했어요. ' + sub
    cells = ''
    for code, (title, sev) in RULES.items():
        state = summary['states'][code]
        label, css = RULE_STATUS[state]
        cells += f'<div class="cell st-{css}"><b>{esc(label)}</b>{esc(RULE_HELP[code][0])}</div>'
    checkpoints = build_checkpoints(data, report, keys, latest, prior, meta)
    checks_html = ''
    if checkpoints:
        checks_html = '<div class="checks"><div class="sec-sub" style="margin:0 0 4px">숫자에서 보이는 확인해 볼 점</div>' + ''.join(
            f'<div class="check"><div class="check-mark {esc(c["kind"])}"></div><div>'
            f'<div class="check-title">{esc(c["title"])}</div><div class="check-detail">{esc(c["detail"])}</div></div></div>'
            for c in checkpoints) + '</div>'
    section('점검 요약')
    block(f'<div class="gap-after"><div class="summary"><div class="summary-head">{esc(head)}</div>'
          f'<div class="summary-sub">{esc(sub)}</div><div class="strip">{cells}</div>{checks_html}</div></div>')
    ask('ask_financial_summary', '재무 조건 점검 결과와 현금흐름을 바탕으로 취업 전에 확인할 점을 설명해줘.', 'stability')

    # ───────────────────────── 탭 ─────────────────────────
    tab_perf, tab_rules, tab_hist, tab_ind, tab_src = st.tabs(
        ['실적·현금흐름', '재무 조건 점검', '5개년 지표', '업종 비교', '자료 출처'])
    present = set(data['financials'])

    with tab_perf:
        section('5년 실적 흐름', None, '억 원')
        ask('ask_revenue', '최근 5년 매출과 영업이익의 흐름과 변동 원인을 설명해줘.')
        left, right = st.columns(2)
        with left, st.container(border=True, height='stretch'):
            block('<div class="m-title">매출액</div>')  # 지표가 하나라 범례 없음
            fig = go.Figure(bars(keys, series(data, keys, 'revenue'), '매출액', C['brand']))
            st.plotly_chart(mark_missing(plot_base(fig), keys, present),
                            config=PLOT_CONFIG, **stretch(st.plotly_chart))
        with right, st.container(border=True, height='stretch'):
            oi_vals, ni_vals = series(data, keys, 'operating_income'), series(data, keys, 'net_income')
            block('<div class="m-title">영업이익 · 당기순이익</div>'
                  + legend_html([('영업이익', shown_colors(oi_vals, C['brand'], C['neg_dark'])),
                                 ('당기순이익', shown_colors(ni_vals, C['second'], C['neg_light']))]))
            fig = go.Figure([bars(keys, oi_vals, '영업이익', C['brand'], C['neg_dark']),
                             bars(keys, ni_vals, '당기순이익', C['second'], C['neg_light'])])
            fig.update_layout(barmode='group')
            st.plotly_chart(mark_missing(plot_base(fig), keys, present),
                            config=PLOT_CONFIG, **stretch(st.plotly_chart))
        section('현금은 어떻게 움직였나',
                '영업은 본업에서 번 현금, 투자는 설비·자산을 사고판 현금, 재무는 빌리고 갚고 배당한 현금이에요.', '억 원')
        ask('ask_cashflow', '최근 5년 영업·투자·재무활동 현금흐름과 영업이익의 차이를 설명해줘.', 'stability')
        cash_series = [('operating_cash_flow', '영업', C['brand']), ('investing_cash_flow', '투자', C['second']),
                       ('financing_cash_flow', '재무', C['third'])]
        with st.container(border=True):
            block(legend_html([(name, [color]) for _, name, color in cash_series]))
            fig = go.Figure()
            for field, name, color in cash_series:
                fig.add_bar(x=keys, y=series(data, keys, field), name=name, marker_color=color,
                            hovertemplate='%{y:,.1f}억 원<extra>' + name + '</extra>')
            fig.update_layout(barmode='group')
            st.plotly_chart(mark_missing(plot_base(fig, height=320), keys, present),
                            config=PLOT_CONFIG, **stretch(st.plotly_chart))
        block('<div class="small">흔히 보이는 모습은 영업 현금이 플러스, 투자 현금이 마이너스(설비·사업에 투자)인 경우예요.<br>'
              '투자·재무 현금이 마이너스라고 곧바로 나쁜 신호는 아니며, 영업 현금이 여러 해 마이너스라면 본업의 현금 창출을 더 살펴볼 필요가 있어요.</div>')

    with tab_rules:
        section('재무 조건 점검', '각 조건이 연도별로 나타났는지 보여 줍니다. 등급이나 부도 가능성을 계산한 결과가 아니에요.')
        rule_years = report['rules']['years']
        for code, (title, sev) in RULES.items():
            q, explain = RULE_HELP[code]
            evidence = flag_evidence(code, rule_years.get(latest))
            yrs = ''
            for k in keys:
                if k not in rule_years:
                    yrs += f'<div class="ry st-unknown">자료 없음<span>{esc(k)}</span></div>'
                    continue
                state = rule_state(code, rule_years[k], sev)
                label = {'risk':'위험', 'watch':'주의', 'clear':'해당 없음', 'unknown':'확인 불가'}[state]
                yrs += f'<div class="ry st-{state}">{label}<span>{esc(k)}</span></div>'
            ev_html = (f'<div class="rule-evidence"><span class="ev-tag">근거</span><span>{esc(evidence)}</span></div>'
                       if evidence else '')
            block(f'<div class="rule"><div><div class="rule-name">{esc(q)}</div>'
                  f'<div class="rule-explain">{esc(explain)}</div>{ev_html}</div>'
                  f'<div class="rule-years">{yrs}</div></div>')
            ask(f'ask_rule_{code}', f'재무 조건 점검의 “{q}” 항목이 무엇을 뜻하는지, 연도별 결과와 근거를 설명해줘.', 'stability')
        block('<div class="small">확인 불가는 필요한 계정이 공시 자료에서 확인되지 않았거나, 연속 2개년 비교에 필요한 직전 연도 자료가 없는 경우예요.<br>'
              '"해당 없음"은 해당 조건이 나타나지 않았다는 뜻이지 회사가 안전하다는 판정이 아닙니다.</div>')

    with tab_hist:
        section('지표별 5개년 변화')
        group = safe_group(st.segmented_control('영역', list(GROUPS), default='수익성', key='metric_group',
                                                label_visibility='collapsed'))
        block(f'<div class="sec-sub">{esc(GROUP_INTRO[group])}</div>')
        summaries = (report.get('trends') or {}).get('summaries') or {}

        def hist_ctx(metric):
            name, meaning, formula, note = METRIC_HELP[metric]
            vals = [report['ratios']['years'].get(k, {}).get('ratios', {}).get(metric, {}).get('value') for k in keys]
            is_cash = metric in CASH_METRICS
            return {'metric': metric, 'name': name, 'meaning': meaning, 'formula': formula, 'note': note,
                    'vals': vals, 'is_cash': is_cash, 'unit': '억 원' if is_cash else '%',
                    'plot_vals': [v / 1e8 if (v is not None and is_cash) else v for v in vals],
                    'changes': growth_changes(metric)}

        def hist_head(c):
            trend = f'{latest}년 값, {trend_sentence(summaries.get(c["metric"]), "KRW" if c["is_cash"] else "%")}'
            latest_change = c['changes'].get(latest)
            if ratios.get(c['metric'], {}).get('value') is None and latest_change:
                value = f'{esc(latest_change["label"])} <span class="m-delta">{esc(latest_change["delta"])}</span>'
            else:
                value = esc(fmt_metric(c['metric'], ratios.get(c['metric'], {}).get('value')))
            block(f'<div class="m-title">{esc(c["name"])}</div><div class="m-value">{value}</div>'
                  f'<div class="m-trend">{with_unit(trend, c["unit"])}</div>')

        def hist_chart(c):
            plot_vals, is_cash = c['plot_vals'], c['is_cash']
            fig = go.Figure(go.Scatter(x=keys, y=plot_vals, mode='lines+markers+text', connectgaps=False,
                                       text=[('' if v is None else (f'{v:,.0f}' if is_cash else f'{v:,.1f}')) for v in plot_vals],
                                       textposition=label_positions(plot_vals),
                                       textfont=dict(size=11, color=C['ink']), cliponaxis=False,
                                       line=dict(color=C['brand'], width=2.5),
                                       marker=dict(color=C['brand'], size=7), name=c['name'],
                                       hovertemplate='%{y:,.1f}' + c['unit'] + '<extra></extra>'))
            fig = plot_base(fig, height=220)
            fig.update_layout(margin=dict(l=4, r=12, t=22, b=4))
            fig.update_yaxes(tickformat=',.0f' if is_cash else ',.1f')
            valid = [v for v in plot_vals if v is not None]
            if valid:  # 점 위 숫자가 위쪽 경계에 잘리지 않도록 위쪽 여유를 더 준다
                lo, hi = min(valid), max(valid)
                span = (hi - lo) or max(abs(hi), 1)
                fig.update_yaxes(range=[lo - span * .25, hi + span * .25])
            else:  # 값이 하나도 없으면 빈 눈금 대신 연도 축과 변화 유형만 보여 준다
                fig.update_yaxes(visible=False, range=[0, 1])
            mark_missing(fig, keys, present)
            # 증가율을 계산하지 않은 연도(전기 0 이하): 변화 유형과 금액 변화를 차트에 표시한다. 예: "적자 축소 +150.0억"
            for k, ch in c['changes'].items():
                fig.add_annotation(x=keys.index(k), yref='paper', y=0.5 if not valid else 0.1, showarrow=False,
                                   text=f'<b>{esc(ch["label"])}</b><br>{esc(ch["delta"])}', align='center',
                                   font=dict(size=11, color=C['ink']), bgcolor='#EEF2F7', borderpad=4)
            st.plotly_chart(fig, config=PLOT_CONFIG, **stretch(st.plotly_chart), key=f'hist_{c["metric"]}')

        def hist_meaning(c):
            block(f'<div class="m-meaning">{esc(c["meaning"])}</div>')

        def hist_how(c):
            # key에 영역을 넣어, 다른 영역으로 옮기면 펼침 상태가 이어지지 않고 기본(닫힘)으로 보이게 한다.
            with st.expander('계산 방법과 읽을 때 주의할 점', key=f'how_{group}_{c["metric"]}'):
                rows = ''
                for k, v in zip(keys, c['vals']):
                    if v is not None:
                        continue
                    ch = c['changes'].get(k)
                    if ch:  # 예: [2025] [적자 축소] −388.1억 → −237.6억
                        tag, text = ch['label'], ch['path']
                    elif k not in present:
                        tag, text = None, '같은 기준의 재무제표 없음'
                    else:
                        tag, text = split_reason(report['ratios']['years'][k]['ratios'].get(c['metric'], {}).get('reason'))
                    rows += (f'<div class="miss-row"><span class="tag-year">{esc(k)}</span>'
                             + (f'<span class="tag-acct">{esc(tag)}</span>' if tag else '')
                             + f'<span class="miss-text">{esc(text)}</span></div>')
                block(f'<div class="how"><p><b>계산</b> {esc(c["formula"])}</p><p><b>주의</b> {esc(c["note"])}</p>'
                      + (f'<div class="miss-title">값이 비어 있는 연도</div>{rows}' if rows else '') + '</div>')

        def hist_ask(c):
            ask(f'ask_metric_{c["metric"]}', f'재무 지표 {c["name"]}의 의미와 최근 5개년 변화, 계산 기준과 주의할 점을 설명해줘.')

        metrics = GROUPS[group]
        for row_start in range(0, len(metrics), 2):
            pair = [(f'{group}_{m}', hist_ctx(m)) for m in metrics[row_start:row_start + 2]]
            card_bands(pair, [hist_head, hist_chart, hist_meaning, hist_how] + ([hist_ask] if on_ask else []))
        # key가 tbl_로 시작하면 펼침 영역 배경을 페이지 배경색으로 칠한다(CSS). 영역마다 key가 달라 닫힘 상태로 시작한다.
        with st.expander('모든 지표를 표로 보기', key=f'tbl_metrics_{group}'):
            rows = []
            for metric in [m for g in GROUPS.values() for m in g]:
                changes = growth_changes(metric)
                values = [changes[k]['label'] if (v is None and k in changes) else fmt_metric(metric, v)
                          for k, v in ((k, report['ratios']['years'].get(k, {}).get('ratios', {}).get(metric, {}).get('value'))
                                       for k in keys)]
                rows.append([esc(METRIC_HELP[metric][0])] + [esc(v) for v in values])
            block(html_table(['지표'] + [esc(k) for k in keys], rows, align=['l'] + ['r'] * len(keys), row_header=True))

    with tab_ind:
        section('비슷한 업종의 회사들과 비교', '한국은행 기업경영분석 통계의 업종 중앙값과 순위 25~75% 구간을 기준으로 이 회사가 어디쯤인지 보여 줍니다.')
        ask('ask_industry', '업종 비교 결과와 한국은행 통계의 기준을 설명하고 연결·별도재무제표 차이와 비교의 한계를 알려줘.')
        matches = [(m, (comp.get('metrics') or {}).get(m)) for m in SERVICE_BENCHMARK_METRICS]
        matches = [(m, x) for m, x in matches if x and x.get('status') == 'reference_only']
        is_holding = induty.startswith(HOLDING_COMPANY_KSIC)
        if not matches and is_holding:
            # 한국은행 기업경영분석에는 금융·보험업(K) 분류가 없어 지주회사(64992)는 맞는 업종 통계가 없다.
            st.info('지주회사는 한국은행 기업경영분석 통계에 해당 업종이 없어 업종 비교를 하지 않아요. '
                    '지주회사의 별도재무제표 매출은 자회사 배당·브랜드 사용료 중심이라 일반 사업회사와 비교하기도 어려워요. '
                    '그룹 전체 실적은 다른 탭(연결재무제표 기준)에서 보세요.')
        elif not matches:
            st.info('이 회사에 맞는 업종 통계를 찾지 못해 비교를 보여 드릴 수 없어요. 회사 자체의 재무 분석은 다른 탭에서 그대로 볼 수 있습니다.')
            reason = industry_info.get('download_error') or industry_info.get('reason') or comp.get('note')
            if reason:
                st.caption(mask_secret(reason))
        else:
            first = matches[0][1]
            basis = [f'업종 {first.get("industry_name") or first.get("benchmark_industry_code")}',
                     f'{first.get("benchmark_year")}년 통계',
                     {'A': '전체 규모', 'L': '대기업', 'M': '중소기업'}.get(first.get('benchmark_company_size'), '규모 미확인')]
            # 업종 비교에 실제로 쓴 재무제표 기준(예전 결과에는 값이 없어 분석 기준으로 대신함)
            comp_basis = comp.get('basis_fs_div') or data['fs_div']
            basis_chip = ('<span class="chip strong">별도재무제표 기준</span>' if comp_basis == 'OFS'
                          else '<span class="chip strong">연결재무제표 기준</span>')
            block('<div class="chips" style="margin:0 0 12px">' + basis_chip
                  + ''.join(f'<span class="chip">{esc(b)}</span>' for b in basis) + '</div>')
            if comp_basis == 'OFS' and data['fs_div'] == 'CFS':
                st.caption('업종 통계(한국은행 기업경영분석)와 같은 별도재무제표(회사 단독) 기준으로 계산했어요.  \n'
                           '자회사를 합친 연결재무제표 기준인 다른 탭의 수치와는 다를 수 있어요.')
            elif comp_basis == 'CFS':
                st.caption('이 회사의 별도재무제표를 찾지 못해 연결재무제표(자회사 포함) 기준으로 비교했어요. '
                           '업종 통계는 별도(개별 기업) 기준이라 차이가 크면 해석에 주의하세요.')
            if is_holding:
                # 업종 통계가 생겨 비교가 되더라도, 지주회사의 별도 매출은 배당 중심이라 성격이 다르다는 점을 알린다.
                st.info('지주회사는 별도재무제표 기준 매출이 자회사 배당·브랜드 사용료 중심이라, 영업이익률·매출 성장률 등이 '
                        '일반 사업회사와 성격이 달라요. 업종 비교는 참고만 하고, 그룹 전체 실적은 다른 탭(연결 기준)에서 보세요.')
            def ind_head(m):
                # 순서: 지표 이름 → 이 지표를 읽는 방향(높을수록/낮을수록) → 이 기업의 위치 문장 → (다음 띠) 그래프
                metric, item = m
                position = quartile_position(item)
                direction = BENCHMARK_DIRECTION_TEXT.get(METRIC_DIRECTION.get(metric), '')
                block(f'<div class="m-title">{esc(METRIC_HELP.get(metric, (metric,))[0])}</div>'
                      + (f'<div class="small">{esc(direction)}</div>' if direction else '')
                      + (f'<div class="pos">{esc(position["lead"])}<b>{esc(position["key"])}</b>{esc(position["tail"])}</div>'
                         if position else ''))

            def ind_note_list(m):
                """그래프 아래에 남는 보충 안내(분위값 미공표·대체 통계·통계 연도 차이). 방향 문구는 제목 아래로 옮겼다."""
                metric, item = m
                notes = []
                if item.get('quantile_coverage') == 'median_only':
                    notes.append('순위 25~75% 구간 값이 공표되지 않아 중앙값과만 비교했어요.')
                if item.get('fallback_used'):
                    notes.append(f'세부 업종·규모 통계가 없어 {item.get("industry_name") or item.get("benchmark_industry_code")} '
                                 f'{ {"A": "전체 규모", "L": "대기업", "M": "중소기업"}.get(item.get("benchmark_company_size"), "") } 통계로 대신 비교했어요.')
                if item.get('benchmark_year') != int(annual['period_end'][:4]):
                    notes.append(f'{item.get("benchmark_year")}년 통계와 비교했어요(회사 자료는 {annual["period_end"][:4]}년).')
                return notes

            def ind_chart(m):
                st.plotly_chart(quartile_figure(m[1]), config=PLOT_CONFIG, **stretch(st.plotly_chart),
                                key=f'quartile_{m[0]}')

            def ind_bench(m):
                block(bench_html(m[1]))

            def ind_notes(m):
                notes = ind_note_list(m)
                if notes:
                    block('<div class="small">' + '<br>'.join(esc(n) for n in notes) + '</div>')

            def ind_ask(m):
                name = METRIC_HELP.get(m[0], (m[0],))[0]
                ask(f'ask_industry_{m[0]}', f'업종 비교에서 {name}의 기업 수치와 중앙값, 재무제표 기준과 해석의 한계를 설명해줘.')

            for row_start in range(0, len(matches), 2):
                pair = [(f'ind_{m}', (m, item)) for m, item in matches[row_start:row_start + 2]]
                # 보충 안내가 좌우 모두 없으면 빈 띠를 만들지 않고 수치·범례 띠에서 카드를 닫는다.
                bands = [ind_head, ind_chart, ind_bench] + ([ind_notes] if any(ind_note_list(p) for _, p in pair) else [])
                if on_ask:
                    bands.append(ind_ask)
                card_bands(pair, bands)
            block('<div class="small" style="margin-top:8px">업종 내 위치는 통계상 상대적인 자리일 뿐 재무 건전성 등급이나 정확한 순위가 아니에요. '
                  '통계의 표본·산식·회계 기준이 이 회사 공시와 다를 수 있습니다.</div>')

    with tab_src:
        section('어떤 자료로 계산했나요', '모든 수치는 아래 사업보고서의 재무제표 원문에서 가져왔습니다.')
        ask('ask_sources', '재무 분석의 자료 출처, 사업연도와 연결·별도 기준, 공시 수치가 달라질 수 있는 이유를 설명해줘.')
        rows = []
        for y in meta['business_year_window']:
            s = meta['year_status'].get(str(y), {})
            rc = s.get('rcept_no') if isinstance(s.get('rcept_no'), str) else None
            link = (f'<a href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo={esc(rc)}" target="_blank" '
                    f'rel="noopener noreferrer">DART에서 열기</a>' if rc else '—')
            rows.append([esc(y), link])
        block(html_table(['사업연도', '원문 보기'], rows, align=['c', 'c']))
        restated = meta.get('consistency_checks') or []
        if restated:
            section('다음 해 보고서에서 달라진 수치',
                    '재작성·재분류 가능성을 알려 주는 정보예요. 이 화면은 각 연도 보고서에 처음 실린 당기 수치를 사용합니다.')
            block(html_table(['연도', '계정', '처음 보고', '다음 해 보고서', '차이'],
                             [[esc(c['year']), esc(FIELD_LABEL.get(c['field'], c['field'])), esc(money(c['original_value'])),
                               esc(f'{money(c["restated_value"])} ({c["reported_in"]})'), esc(f'{c["difference_pct"]:+.1f}%')]
                              for c in restated],
                             align=['c', 'l', 'r', 'r', 'r']))
        with st.expander(f'{latest}년 계정별 확인 결과', key='tbl_accounts'):
            fields = (meta.get('accounts_audit') or {}).get(latest, {}).get('fields', {})
            block(html_table(['계정', '공시 계정명', '금액'],
                             [[esc(FIELD_LABEL.get(f, f)), esc(v.get('account_nm') or '—'), esc(money(v.get('value')))]
                              for f, v in fields.items()],
                             align=['l', 'l', 'r']))
        block(f'<div class="small">기준 : OpenDART 사업보고서(11011), {esc(fs_label)}. '
              '5개년 모두 같은 기준을 쓰고, 없는 연도·계정은 0으로 채우지 않았어요.</div>')
