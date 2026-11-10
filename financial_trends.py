"""STEP 4: 연속된 사업연도의 재무지표 방향과 변화폭을 분석한다.

재무안전 등급이나 업종별 상대평가는 수행하지 않는다.
None(미계산)을 0으로 취급하지 않으며, 끊어진 회계기간을 연결하지 않는다.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .financial_ratios import RATIO_LABELS, calculate_financial_ratios
from .schemas import FinancialData

# 값이 높아지는 것을 일반적으로 우호적으로 해석할 수 있는 지표.
# 단, 이는 단순 변화 방향이며 기업의 종합 안전성 또는 위험 등급이 아니다.
# v3.1: 총자산·유형자산 증가율은 차입·인수로도 커질 수 있어 방향만으로 '개선'이라 볼 수 없다.
# 업종 비교 모듈(METRIC_DIRECTION='contextual')과 해석 정책을 일치시킨다.
FAVORABLE_UP = {
    'current_ratio', 'equity_ratio', 'operating_margin', 'net_margin',
    'roa', 'operating_cash_flow', 'cash_flow_to_debt',
    'revenue_growth', 'operating_income_growth',
}
FAVORABLE_DOWN = {'debt_ratio', 'capital_impairment_ratio'}


def _direction(change: float | int) -> str:
    return 'increase' if change > 0 else 'decrease' if change < 0 else 'unchanged'


def _interpret_direction(metric: str, direction: str) -> str:
    if direction == 'unchanged':
        return 'unchanged'
    if metric in FAVORABLE_UP:
        return 'improving' if direction == 'increase' else 'deteriorating'
    if metric in FAVORABLE_DOWN:
        return 'improving' if direction == 'decrease' else 'deteriorating'
    return 'not_assessed'


def analyze_financial_trends(
    data: FinancialData,
    ratios_result: dict[str, Any] | None = None,
    *,
    max_years: int = 5,
) -> dict[str, Any]:
    """연도별 지표의 직전 회계기간 대비 변화 및 연속 관측 구간 요약.

    max_years: 최신 결산기부터 포함할 최대 사업연도 수(2 이상).
    비율 지표 변화는 %p, 영업현금흐름 변화는 KRW로 반환한다.
    """
    if max_years < 2:
        raise ValueError('max_years는 2 이상이어야 합니다.')
    if ratios_result is None:
        ratios_result = calculate_financial_ratios(data)
    if (ratios_result.get('corp_code') != data['corp_code']
            or ratios_result.get('fs_div') != data['fs_div']
            or set(ratios_result.get('years', {})) != set(data['financials'])):
        raise ValueError('비율 계산 결과와 원본 데이터의 기업/재무제표/연도가 일치하지 않습니다.')

    ordered = sorted(data['financials'].items(), key=lambda x: x[1]['period_end'])[-max_years:]
    result_years: dict[str, Any] = {}
    prev_key = None
    prev_annual = None
    for key, annual in ordered:
        end = date.fromisoformat(annual['period_end'])
        start = date.fromisoformat(annual['period_start'])
        if start > end:
            raise ValueError(f'{key}: 회계기간 날짜가 잘못되었습니다.')
        if key not in ratios_result['years']:
            raise ValueError(f'{key}: 지표 계산 결과가 없습니다.')
        prev_comparable = (
            prev_annual is not None and
            date.fromisoformat(prev_annual['period_end']) + timedelta(days=1) == start
        )
        details: dict[str, Any] = {}
        for metric, label in RATIO_LABELS.items():
            latest_item = ratios_result['years'][key]['ratios'][metric]
            latest = latest_item['value']
            unit = 'KRW' if metric in {'operating_cash_flow', 'investing_cash_flow', 'financing_cash_flow'} else '%p'
            if not prev_comparable:
                reason = '비교할 직전 연속 회계연도가 없음'
                if prev_annual is not None:
                    reason = '회계기간이 연속되지 않아 비교 불가'
                details[metric] = {'label': label, 'current_value': latest, 'previous_value': None,
                                   'change': None, 'change_unit': unit, 'direction': 'unknown',
                                   'interpretation': 'not_assessed', 'reason': reason}
                continue
            previous = ratios_result['years'][prev_key]['ratios'][metric]['value']
            if latest is None or previous is None:
                details[metric] = {'label': label, 'current_value': latest, 'previous_value': previous,
                                   'change': None, 'change_unit': unit, 'direction': 'unknown',
                                   'interpretation': 'not_assessed',
                                   'reason': '현재 또는 직전 연도 지표가 계산되지 않음'}
                continue
            delta = latest - previous
            if unit == '%p':
                delta = round(delta, 2)
            direction = _direction(delta)
            details[metric] = {'label': label, 'current_value': latest, 'previous_value': previous,
                               'change': delta, 'change_unit': unit, 'direction': direction,
                               'interpretation': _interpret_direction(metric, direction), 'reason': None}
        result_years[key] = {'period_end': annual['period_end'], 'previous_year': prev_key if prev_comparable else None,
                             'metrics': details}
        prev_key, prev_annual = key, annual

    latest_year = ordered[-1][0] if ordered else None
    # 최근 연속 구간만 사용하여 지속적인 상승/하락 경향 요약(3개년 이상).
    summaries = {}
    for metric, label in RATIO_LABELS.items():
        observed = []
        for key, annual in reversed(ordered):
            value = ratios_result['years'][key]['ratios'][metric]['value']
            if value is None:
                break
            if observed:
                newer_key = observed[-1][0]
                newer = data['financials'][newer_key]
                if date.fromisoformat(annual['period_end']) + timedelta(days=1) != date.fromisoformat(newer['period_start']):
                    break
            observed.append((key, value))
        observed.reverse()
        if len(observed) < 3:
            summaries[metric] = {'label': label, 'status': 'insufficient_data', 'years_used': [k for k, _ in observed],
                                 'reason': '최신 연도까지 연속된 유효 관측치가 3개 미만'}
            continue
        values = [v for _, v in observed]
        diffs = [b - a for a, b in zip(values, values[1:])]
        if all(d > 0 for d in diffs):
            status = 'consistently_increasing'
        elif all(d < 0 for d in diffs):
            status = 'consistently_decreasing'
        elif all(d == 0 for d in diffs):
            status = 'unchanged'
        else:
            status = 'mixed'
        overall_change = values[-1] - values[0]
        if metric not in {'operating_cash_flow', 'investing_cash_flow', 'financing_cash_flow'}:
            overall_change = round(overall_change, 2)
        summaries[metric] = {'label': label, 'status': status, 'years_used': [k for k, _ in observed],
                             'first_value': values[0], 'latest_value': values[-1],
                             'overall_change': overall_change,
                             'change_unit': 'KRW' if metric in {'operating_cash_flow', 'investing_cash_flow', 'financing_cash_flow'} else '%p',
                             'reason': None}

    return {'corp_code': data['corp_code'], 'corp_name': data['corp_name'],
            'fs_div': data['fs_div'], 'latest_year': latest_year,
            'years': result_years, 'summaries': summaries,
            'note': '변화 방향은 기술적 분석이며 재무위험 판정 또는 업종별 평가가 아님'}
