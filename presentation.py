"""UI 표현 정책: 계산/데이터 처리와 분리, 재무안전등급으로 오인하지 않도록 표시.

v3.1 변경 요약
- 화면 문구·상태 판정 로직을 app.py에서 분리해 단위 테스트가 가능하게 했다.
- 위험 조건 '미해당'을 초록(안전)으로 표시하지 않는다: 'clear' → 중립 회색 '해당 없음'.
- 전년 대비 증감은 빨강/초록 대신 ▲/▼ 기호와 중립색으로 표시한다
  (한국 증시 관행은 상승=빨강이라 색 의미가 사용자마다 반대로 읽힐 수 있음).
- 이익 지표는 '흑자 전환/적자 지속' 같은 상태 변화를 별도로 보여준다
  (전기 0 이하이면 증가율이 비어 정보가 사라지던 문제 보완).
- 결정론적 '확인해 볼 점' 생성기: 계산된 수치만으로 사실을 서술하고 평가·예측하지 않는다.
"""
from __future__ import annotations
import math
from typing import Any

METRIC_HELP = {
 'debt_ratio':('부채비율','자기자본에 비해 부채가 얼마나 큰지 보여줘요.','부채총계 ÷ 자본총계 × 100','높을수록 재무 부담이 클 수 있지만 업종 특성도 확인해야 해요.'),
 'current_ratio':('단기 지급여력(유동비율)','1년 안에 현금화 가능한 자산과 갚아야 할 부채의 비율이에요.','유동자산 ÷ 유동부채 × 100','유동자산이 모두 현금은 아닙니다.'),
 'equity_ratio':('자기자본비율','전체 자산 중 빌리지 않은 자본이 차지하는 비중이에요.','자본총계 ÷ 자산총계 × 100','자기자본이 높더라도 손실이나 현금 부족은 발생할 수 있어요.'),
 'capital_impairment_ratio':('자본잠식률','누적 손실로 자본이 주주가 넣은 원금(자본금)보다 얼마나 줄었는지 보여줘요.',
                             '(자본금 − 지배기업 소유주지분) ÷ 자본금 × 100',
                             '연결재무제표는 비지배지분을 뺀 지배기업 몫 자본으로 계산해요. 자본이 자본금보다 크면 0%로 표시합니다. '
                             '거래소는 사업연도말 잠식률 50% 이상을 관리종목, 전액잠식을 상장폐지 요건으로 씁니다.'),
 'operating_margin':('영업이익률','매출 100원 중 본업에서 남긴 이익이 얼마인지 나타내요.','영업이익 ÷ 매출액 × 100','다른 업종과 비교할 때는 업종별 이익 구조가 다르다는 점을 확인하세요.'),
 'net_margin':('순이익률','매출 100원 중 최종적으로 남은 이익이 얼마인지 나타내요.','당기순이익 ÷ 매출액 × 100','일회성 이익이나 손실이 영향을 줄 수 있어요.'),
 'roa':('자산 활용 수익률(ROA)','기업의 자산을 활용해 얼마나 이익을 냈는지 보여줘요.','당기순이익 ÷ 평균 총자산 × 100','전년 자산이 없으면 계산할 수 없습니다.'),
 'revenue_growth':('매출 성장률','작년보다 매출이 얼마나 늘거나 줄었는지 보여줘요.','(올해 매출 − 작년 매출) ÷ 작년 매출 × 100','매출이 성장해도 수익성이 악화될 수 있어요.'),
 'operating_income_growth':('영업이익 성장률','본업 이익이 직전 연도보다 얼마나 달라졌는지 나타내요.','(올해 영업이익 − 작년 영업이익) ÷ 작년 영업이익 × 100','전년 이익이 0 이하이면 통상적인 증가율을 계산하지 않아요.'),
 'total_asset_growth':('자산 규모 변화','회사가 보유한 자산이 전년보다 얼마나 변했는지 보여줘요.','(올해 자산 − 전년 자산) ÷ 전년 자산 × 100','자산은 차입이나 인수로도 늘어나므로 증가 자체를 좋다·나쁘다로 보지 않아요.'),
 'ppe_growth':('설비·유형자산 변화','건물·기계 등의 장부금액이 얼마나 변했는지 보여줘요.','(올해 유형자산 − 전년 유형자산) ÷ 전년 유형자산 × 100','감가상각과 매각도 금액 변화에 영향을 줍니다.'),
 'cash_flow_to_debt':('부채 대비 영업현금 창출력','전체 부채와 비교해 본업에서 실제로 들어온 현금의 규모예요.','영업활동현금흐름 ÷ 부채총계 × 100','이 비율로 부채를 몇 년 만에 상환할 수 있다고 단정하면 안 됩니다.'),
 'operating_cash_flow':('영업활동 현금흐름','본업의 판매·서비스 과정에서 실제 오간 현금이에요.','현금흐름표의 영업활동현금흐름','장기간 음수라면 본업의 현금 창출 상태를 살펴봐야 해요.'),
 'investing_cash_flow':('투자활동 현금흐름','설비·사업·금융자산에 투자하거나 처분하면서 오간 현금이에요.','현금흐름표의 투자활동현금흐름','음수는 투자가 많다는 뜻일 수도 있어 곧바로 나쁜 신호는 아니에요.'),
 'financing_cash_flow':('재무활동 현금흐름','돈을 빌리거나 갚고, 배당·증자 등을 하면서 오간 현금이에요.','현금흐름표의 재무활동현금흐름','플러스는 신규 차입 때문일 수 있고 마이너스는 상환 때문일 수 있어요.'),
}

RULE_HELP = {
 # 판정 기준(잠식률 50% 이상)을 질문에 담는다. '자본잠식이 있나요?'로 쓰면 50% 미만 부분잠식이
 # '해당 없음'으로 나올 때 질문과 결과가 어긋나 보이기 때문.
 'CAPITAL_IMPAIRMENT':('자본이 절반 이상 잠식됐나요?','누적 손실로 자본이 자본금보다 얼마나 줄었는지 봅니다. '
                       '절반 이상 줄었으면 주의, 모두 사라졌으면 위험으로 표시해요.'),
 'OPERATING_LOSS':('본업에서 손실이 났나요?','판매·서비스로 얻은 영업이익이 적자인지 확인합니다.'),
 'NET_LOSS':('최종적으로 적자였나요?','이자·세금 등을 반영한 최종 순이익이 적자인지 확인합니다.'),
 'NEGATIVE_OPERATING_CASH_FLOW':('본업에서 현금이 빠져나갔나요?','영업활동에서 현금 유출이 유입보다 많았는지 확인합니다.'),
 'REPEATED_OPERATING_LOSS':('영업적자가 2년 연속인가요?','같은 재무제표 기준으로 연속 두 회계연도에 영업손실이 났는지 확인합니다.'),
 'REPEATED_NEGATIVE_CASH_FLOW':('영업현금이 2년 연속 부족했나요?','연속 두 회계연도에 영업현금흐름이 음수였는지 확인합니다.'),
}

GROUPS = {
 '수익성':['operating_margin','net_margin','roa'],
 '재무 안정성':['debt_ratio','current_ratio','equity_ratio','capital_impairment_ratio'],
 '성장성':['revenue_growth','operating_income_growth','total_asset_growth','ppe_growth'],
 '현금흐름':['operating_cash_flow','investing_cash_flow','financing_cash_flow','cash_flow_to_debt'],
}
GROUP_INTRO = {
 '수익성':'매출에서 얼마나 남기는지 봅니다.',
 '재무 안정성':'빚과 자기 돈의 균형, 단기 지급 여력을 봅니다.',
 '성장성':'회사 규모와 실적이 커지고 있는지 봅니다.',
 '현금흐름':'장부상 이익이 아니라 실제 현금이 어떻게 움직였는지 봅니다.',
}
CASH_METRICS = {'operating_cash_flow','investing_cash_flow','financing_cash_flow'}
SERVICE_BENCHMARK_METRICS = ('debt_ratio','operating_margin','net_margin','revenue_growth','total_asset_growth','ppe_growth')
# 업종 비교에서 값이 낮을수록 부담이 작은 지표 / 높을수록 유리한 지표 / 맥락 필요
BENCHMARK_DIRECTION_TEXT = {
 'low':'낮을수록 재무 부담이 작은 편으로 봅니다.',
 'high':'높을수록 유리한 편으로 봅니다.',
 'contextual':'높고 낮음만으로 좋고 나쁨을 판단하지 않습니다.',
}

# 위험 조건 상태 표시 정책. 'clear'는 초록이 아니라 중립색이다.
RULE_STATUS = {
 'risk':('해당 · 위험','risk'),
 'watch':('해당 · 주의','watch'),
 'clear':('해당 없음','clear'),
 'unknown':('확인 불가','unknown'),
}


def safe_group(value: str | None, default: str = '수익성') -> str:
    """segmented_control은 선택 해제 시 None을 돌려준다. KeyError 대신 기본 영역으로 되돌린다."""
    return value if value in GROUPS else default


def money(v) -> str:
    if v is None:
        return '—'
    sign = '−' if v < 0 else ''
    a = abs(v)
    if a >= 1e12:
        return f'{sign}{a/1e12:,.2f}조 원'
    if a >= 1e8:
        return f'{sign}{a/1e8:,.1f}억 원'
    if a >= 1e4:
        return f'{sign}{a/1e4:,.0f}만 원'
    return f'{sign}{a:,.0f}원'


def pct(v) -> str:
    return '—' if v is None else f'{v:,.1f}%'


def signal_for_delta(current, previous, unit='money'):
    """전년 대비 단순 변화 방향, 안전/위험 *등급*이 아님."""
    if current is None or previous is None:
        return {'kind':'neutral','label':'전년 비교 자료 없음'}
    if unit == 'ratio':
        delta = current - previous
        return {'kind':'positive' if delta>0 else 'negative' if delta<0 else 'neutral',
                'label':f'전년 대비 {delta:+.2f}%p'}
    if previous <= 0:
        return {'kind':'neutral','label':'전년 값이 0 이하로 증가율 미표시'}
    delta = (current - previous) / previous * 100
    return {'kind':'positive' if delta>0 else 'negative' if delta<0 else 'neutral',
            'label':f'전년 대비 {delta:+.1f}%'}


def delta_display(current, previous, unit='money') -> dict[str, str]:
    """KPI 카드용: 방향 기호(▲▼)와 중립 문구. 색으로 좋고 나쁨을 암시하지 않는다."""
    base = signal_for_delta(current, previous, unit)
    arrow = {'positive':'▲','negative':'▼'}.get(base['kind'], '')
    label = base['label'].replace('전년 대비 ', '')
    if base['kind'] == 'neutral' and current is not None and previous is not None and current == previous:
        label = '변동 없음'
    return {'arrow':arrow, 'label':label, 'kind':base['kind']}


def profit_transition(current, previous) -> str | None:
    """이익 계정의 상태 변화. 증가율로 설명되지 않는 부호 변화를 문장으로 표시."""
    if current is None or previous is None:
        return None
    if previous < 0 <= current:
        return '흑자 전환' if current > 0 else '손익분기'
    if previous >= 0 > current:
        return '적자 전환'
    if previous < 0 and current < 0:
        return '적자 축소' if current > previous else '적자 확대' if current < previous else '적자 지속'
    return None


def rule_state(code: str, year_row: dict | None, severity: str) -> str:
    """연도별 규칙 결과 → risk/watch/clear/unknown.

    v3.3: 같은 규칙이라도 플래그마다 심각도가 다를 수 있다(자본잠식 50% 이상=warning, 전액잠식=critical).
    플래그에 심각도가 있으면 그것을, 없으면 규칙 기본값을 쓴다.
    """
    if not year_row:
        return 'unknown'
    hit = next((f for f in year_row.get('flags', []) if f['code'] == code), None)
    if hit is not None:
        return 'risk' if hit.get('severity', severity) == 'critical' else 'watch'
    if any(u['code'] == code for u in year_row.get('unassessed', [])):
        return 'unknown'
    return 'clear'


def rules_summary(rules_result: dict, year: str, rules: dict) -> dict[str, Any]:
    row = (rules_result.get('years') or {}).get(year) or {}
    states = {code: rule_state(code, row, sev) for code, (_, sev) in rules.items()}
    return {
        'total': len(states),
        'flagged': sum(s in ('risk','watch') for s in states.values()),
        'critical': sum(s == 'risk' for s in states.values()),
        'unknown': sum(s == 'unknown' for s in states.values()),
        'clear': sum(s == 'clear' for s in states.values()),
        'states': states,
    }


IMPAIRMENT_LEVEL_TEXT = {
    'full': '전액잠식',
    'severe': '부분잠식 · 50% 이상',
    'partial': '부분잠식 · 50% 미만',
}


def impairment_evidence(info: dict | None) -> str | None:
    """자본잠식 근거 문장. 잠식이 있을 때만(50% 미만 부분잠식 포함) 만든다."""
    if not info or info.get('level') not in IMPAIRMENT_LEVEL_TEXT:
        return None
    equity_name = '지배기업 소유주지분' if info.get('equity_basis') == 'owners_equity' else '자본총계'
    parts = []
    if info.get('issued_capital') is not None:
        parts.append(f"자본금 {money(info['issued_capital'])}")
    parts.append(f"{equity_name} {money(info.get('parent_equity'))}")
    rate = info.get('capital_impairment_ratio')
    head = f"자본잠식률 {pct(rate)}" if rate is not None else '자본 0 이하'
    return f"{head} ({IMPAIRMENT_LEVEL_TEXT[info['level']]}) · {', '.join(parts)}"


def flag_evidence(code: str, year_row: dict | None) -> str | None:
    """해당 조건의 근거 금액 문장."""
    if code == 'CAPITAL_IMPAIRMENT':
        return impairment_evidence((year_row or {}).get('capital_impairment'))
    for f in (year_row or {}).get('flags', []):
        if f['code'] != code:
            continue
        ev = f.get('evidence', {})
        if 'previous_value' in ev:
            return f"{ev.get('previous_year')}년 {money(ev['previous_value'])} → {f['year']}년 {money(ev['current_value'])}"
        vals = [money(v) for k, v in ev.items() if k != 'unit' and isinstance(v, (int, float))]
        return vals[0] if vals else None
    return None


def quartile_position(item: dict) -> dict[str, str] | None:
    """업종 내 위치를 '이 기업은 …에 있어요' 문장으로. 사분위가 모두 있을 때만 구간을 말한다.

    v3.5: '하위/상위'는 성적이 나쁘다/좋다로 읽히기 쉽지만 부채비율처럼 낮을수록 유리한 지표도 있다.
    위치만 중립적으로 '값이 낮은 쪽/높은 쪽'으로 말하고, 좋고 나쁨은 카드 아래 방향 문구가 알려 준다.
    반환: text(전체 문장), lead/key/tail(key는 화면에서 굵게), band
    """
    band = item.get('percentile_band')
    parts = {
        'below_p25': ('이 기업은 업종에서 ', '값이 낮은 쪽 25%', ' 구간에 있어요'),
        'p25_to_p50': ('이 기업은 업종 ', '중간보다 약간 낮은 구간(25~50%)', '에 있어요'),
        'p50_to_p75': ('이 기업은 업종 ', '중간보다 약간 높은 구간(50~75%)', '에 있어요'),
        'above_p75': ('이 기업은 업종에서 ', '값이 높은 쪽 25%', ' 구간에 있어요'),
    }
    if band not in parts:
        diff = item.get('difference_from_median_pp')
        if diff is None:
            return None
        if diff == 0:
            band, chosen = 'median', ('이 기업은 업종 ', '중앙값과 같아요', '')
        else:
            band, chosen = 'median', ('이 기업은 업종 ', '중앙값보다 ' + ('높아요' if diff > 0 else '낮아요'),
                                      ' (분위 구간 정보 없음)')
    else:
        chosen = parts[band]
    lead, key, tail = chosen
    return {'text': lead + key + tail, 'lead': lead, 'key': key, 'tail': tail, 'band': band}


# 성장률: 전기 금액이 0 이하이면 증가율(%)을 계산하지 않는다(한국은행 기업경영분석·애널리스트 관행).
# 대신 변화 유형과 금액 변화를 보여 준다. 절댓값 분모(÷|전기|) 방식은 쓰지 않는다.
GROWTH_BASE_FIELD = {'revenue_growth': 'revenue', 'operating_income_growth': 'operating_income'}
CHANGE_TYPE_TEXT = {
    'turnaround_to_profit': '흑자 전환',
    'from_zero_to_positive': '흑자 전환',
    'from_zero_to_negative': '적자 전환',
    'recovered_to_zero': '손익분기',
    'loss_narrowed': '적자 축소',
    'loss_widened': '적자 확대',
    'unchanged_loss': '적자 지속',
    'zero_to_zero': '변동 없음',
}


def eok(v) -> str:
    """억 원 단위 짧은 표기: −388.1억."""
    if v is None:
        return '—'
    return f"{'−' if v < 0 else ''}{abs(v) / 1e8:,.1f}억"


def growth_change(item: dict | None, previous, current) -> dict[str, str] | None:
    """증가율을 계산하지 않은 연도의 변화 유형과 금액 변화. 계산된 연도·사유 불명은 None.

    반환: label('적자 축소'), delta('+150.0억'), path('−388.1억 → −237.6억')
    """
    if not item or item.get('value') is not None or previous is None or current is None:
        return None
    label = CHANGE_TYPE_TEXT.get(item.get('change_type'))
    if not label:
        return None
    diff = current - previous
    delta = f"{'+' if diff > 0 else '−' if diff < 0 else '±'}{abs(diff) / 1e8:,.1f}억"
    return {'label': label, 'delta': delta, 'path': f'{eok(previous)} → {eok(current)}'}


def split_reason(reason: str | None) -> tuple[str | None, str]:
    """'영업이익: 당기 또는 전기 금액 누락' → ('영업이익', '당기 또는 전기 금액 누락'). 계정 이름이 없으면 (None, 사유)."""
    text = (reason or '계산 자료 부족').strip()
    account, sep, rest = text.partition(': ')
    if sep and account and rest and len(account) <= 12:
        return account, rest
    return None, text


def trend_sentence(summary: dict | None, unit: str) -> str:
    """5개년 연속 관측 요약을 한 문장으로."""
    if not summary or summary.get('status') == 'insufficient_data':
        return '연속된 3개년 자료가 부족해 추세를 요약하지 않습니다.'
    years = summary.get('years_used') or []
    span = f'{years[0]}~{years[-1]}년' if years else ''
    status = {
        'consistently_increasing':'계속 높아졌습니다',
        'consistently_decreasing':'계속 낮아졌습니다',
        'unchanged':'변화가 없었습니다',
        'mixed':'오르내림이 섞여 있습니다',
    }.get(summary.get('status'), '추세를 판단하지 않습니다')
    return f'{span} 동안 {status}.'


def _stmt(data: dict, key: str | None, field: str):
    if not key:
        return None
    return ((data.get('financials') or {}).get(key) or {}).get('statement', {}).get(field)


def build_checkpoints(data: dict, report: dict, keys: list[str], latest: str,
                      prior: str | None, meta: dict | None = None) -> list[dict[str, str]]:
    """수치로 확인된 사실만으로 '확인해 볼 점'을 만든다. 판단·예측 표현은 쓰지 않는다.

    kind: 'attention'(눈여겨볼 점) / 'info'(맥락 정보) / 'data'(자료 한계)
    """
    out: list[dict[str, str]] = []
    ni = _stmt(data, latest, 'net_income')
    oi = _stmt(data, latest, 'operating_income')
    ocf = _stmt(data, latest, 'operating_cash_flow')
    fcf = _stmt(data, latest, 'financing_cash_flow')
    if ni is not None and ocf is not None and ni > 0 and ocf < 0:
        out.append({'kind':'attention','title':'이익은 났지만 본업 현금은 빠져나갔어요',
                    'detail':f'{latest}년 순이익 {money(ni)}, 영업활동현금흐름 {money(ocf)}. 매출채권·재고 증가 등 이익과 현금이 다른 이유를 사업보고서에서 확인해 보세요.'})
    if ocf is not None and fcf is not None and ocf < 0 < fcf:
        out.append({'kind':'attention','title':'본업 현금 부족을 외부 자금으로 메웠을 수 있어요',
                    'detail':f'{latest}년 영업현금 {money(ocf)}, 재무활동현금 {money(fcf)}. 차입·증자 내역은 현금흐름표 주석에서 확인할 수 있습니다.'})
    impairment = (((report.get('rules') or {}).get('years') or {}).get(latest) or {}).get('capital_impairment') or {}
    if impairment.get('level') == 'partial':
        out.append({'kind':'attention','title':'자본 일부가 잠식됐어요 (잠식률 50% 미만)',
                    'detail':f"{impairment_evidence(impairment)}. 누적 손실로 자본이 자본금보다 줄어든 상태예요. "
                             '거래소 관리종목 요건(50% 이상)에는 이르지 않았지만, 손실이 이어지면 잠식률이 커질 수 있어요.'})
    t = profit_transition(oi, _stmt(data, prior, 'operating_income'))
    if t in ('흑자 전환', '적자 전환'):
        out.append({'kind':'attention' if t == '적자 전환' else 'info',
                    'title':f'영업이익이 {prior}년 대비 {t}했어요',
                    'detail':'일회성 요인인지, 사업 구조의 변화인지 사업보고서의 사업의 내용·경영진단 의견에서 확인해 보세요.'})
    summaries = (report.get('trends') or {}).get('summaries') or {}
    rev = summaries.get('revenue_growth') or {}
    if rev.get('status') == 'consistently_decreasing':
        out.append({'kind':'info','title':'매출 성장률이 해마다 낮아졌어요',
                    'detail':'성장 속도가 줄어든 것이며, 매출 자체가 줄었다는 뜻은 아닐 수 있어요. 매출액 그래프와 함께 보세요.'})
    comparison = report.get('comparison') or {}
    comp = comparison.get('metrics') or {}
    debt = comp.get('debt_ratio') or {}
    if debt.get('percentile_band') == 'above_p75':
        basis = ' (업종 비교용 별도재무제표 기준)' if comparison.get('basis_fs_div') == 'OFS' and data.get('fs_div') == 'CFS' else ''
        out.append({'kind':'attention','title':'부채비율이 업종 상위 25%보다 높아요',
                    'detail':f"이 기업 {pct(debt.get('value'))}{basis}, 업종 75분위 {pct((debt.get('quantiles') or {}).get('p75'))}. 업종 통계는 산식·표본이 달라 참고용입니다."})
    missing = [k for k in keys if k not in (data.get('financials') or {})]
    if missing:
        out.append({'kind':'data','title':f"{', '.join(missing)}년 자료가 없어요",
                    'detail':'해당 연도는 같은 기준(연결/별도)의 사업보고서 재무제표가 조회되지 않았습니다. 연속 비교가 필요한 항목은 일부 계산되지 않습니다.'})
    restated = (meta or {}).get('consistency_checks') or []
    if restated:
        years = sorted({c['year'] for c in restated})
        out.append({'kind':'data','title':f"{', '.join(years)}년 수치가 다음 해 보고서에서 달라졌어요",
                    'detail':'회계정책 변경·오류 수정·사업 재분류 등으로 과거 수치가 재작성됐을 수 있습니다. 이 화면은 각 연도 보고서의 당기 수치를 그대로 씁니다.'})
    return out


def chart_range(points):
    """분위수 위치축의 실제 수치 범위. 누락 분위수는 추정하지 않는다."""
    valid = [float(v) for v in points if v is not None and math.isfinite(float(v))]
    if not valid:
        return -1.,1.
    lo,hi = min(valid),max(valid)
    pad = max((hi-lo)*.14,abs(lo)*.08,abs(hi)*.08,1.)
    return lo-pad,hi+pad
