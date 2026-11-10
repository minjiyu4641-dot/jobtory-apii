"""한국은행 ECOS의 실제 업종별 사분위수(p25/p50/p75) 비교.

중요: 한국은행 기업경영분석과 DART CFS/OFS는 동일 표본/산식이라고 보장할 수 없다.
따라서 항상 참고 비교(reference_only)로 표시한다. 위험/안전 등급을 산출하지 않는다.
"""
from __future__ import annotations
import csv
import json
import math
from pathlib import Path
from typing import Any
from .financial_ratios import RATIO_LABELS, calculate_financial_ratios
from .schemas import FinancialData

METRIC_DIRECTION = {
    'debt_ratio':'low', 'operating_margin':'high', 'net_margin':'high',
    'revenue_growth':'high', 'total_asset_growth':'contextual',
    'ppe_growth':'contextual', 'interest_coverage_ratio':'high',
    'borrowing_dependency_ratio':'low',
}
# 위 8개는 ECOS 501Y086에서 실제 확인된 계정코드에 한정.
NEEDED = {'industry_code','industry_name','fiscal_year','company_size','metric',
          'p25','p50','p75','source_type','stat_code','unit','accounting_basis',
          'source_url','benchmark_version'}


def load_benchmarks(path: str | Path) -> list[dict[str, Any]]:
    """ECOS CSV 로드. p50 필수, p25/p75는 결측 허용. 구 CSV와 호환."""
    out, seen = [], set()
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        missing = NEEDED - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f'벤치마크 파일 필수 열 누락: {sorted(missing)}')
        for line, raw in enumerate(reader, 2):
            try:
                row = dict(raw)
                row['fiscal_year'] = int(row['fiscal_year'])
                for k in ('p25', 'p50', 'p75'):
                    value = (row.get(k) or '').strip()
                    row[k] = float(value) if value else None
                    if row[k] is not None and not math.isfinite(row[k]):
                        raise ValueError(f'{k} 무한대/NaN')
                if row['p50'] is None:
                    raise ValueError('중앙값(p50) 부재: 비교용 CSV에 저장 불가')
                present = [row[k] for k in ('p25', 'p50', 'p75') if row[k] is not None]
                if any(a > b for a, b in zip(present, present[1:])):
                    raise ValueError('분위수 값/순서 이상')
                row['quantile_coverage'] = ('full_quantile'
                    if all(row[k] is not None for k in ('p25', 'p50', 'p75'))
                    else 'median_only')
                if row['metric'] not in METRIC_DIRECTION:
                    raise ValueError('지원되지 않는 지표')
                if row['source_type'] != 'BOK_ECOS' or row['stat_code'] != '501Y086':
                    raise ValueError('ECOS 501Y086 외 기준은 별도 로더 필요')
                if row['company_size'] not in ('A', 'L', 'M') or row['unit'] != '%':
                    raise ValueError('기업규모/비율 단위 불일치')
                key = (row['industry_code'], row['fiscal_year'], row['company_size'], row['metric'])
                if key in seen:
                    raise ValueError('중복된 기준 행')
                seen.add(key)
                out.append(row)
            except (ValueError, TypeError, KeyError) as e:
                raise ValueError(f'벤치마크 CSV {line}행 오류: {e}') from e
    return out


def _band(value: float, r: dict) -> str | None:
    if r['quantile_coverage'] != 'full_quantile':
        return None  # p25 또는 p75가 없으면 사분위 위치를 단정할 수 없음
    if value < r['p25']: return 'below_p25'
    if value < r['p50']: return 'p25_to_p50'
    if value <= r['p75']: return 'p50_to_p75'
    return 'above_p75'


def _industry_fallback_codes(industry_code: str) -> list[str]:
    """한국표준산업분류 세부 코드→상위 코드. 특수 합계코드(ZZZ)는 자동 매핑하지 않음."""
    if not industry_code or industry_code.startswith('ZZZ'):
        return [industry_code] if industry_code else []
    result = [industry_code]
    parent = industry_code[0]
    if parent != industry_code and parent.isalpha():
        result.append(parent)
    return result


def _candidate_paths(ind: str, size: str, allow_fallback: bool) -> list[tuple[str, str, str]]:
    """우선순위: 동일세부+동일규모 → 동일세부+A → 상위+동일규모 → 상위+A."""
    codes = _industry_fallback_codes(ind)
    candidates = [(ind, size, 'exact')]
    if allow_fallback:
        if size != 'A':
            candidates.append((ind, 'A', 'size_to_all'))
        if len(codes) > 1:
            parent = codes[1]
            candidates.append((parent, size, 'parent_industry'))
            if size != 'A':
                candidates.append((parent, 'A', 'parent_and_size_to_all'))
    return list(dict.fromkeys(candidates))


def compare_industry(data: FinancialData, benchmarks: list[dict[str, Any]],
                     ratios_result: dict[str, Any] | None = None, *, year: str | None = None,
                     company_size: str = 'A', max_reference_gap: int = 1,
                     industry_code: str | None = None, allow_fallback: bool = True) -> dict[str, Any]:
    """지표별 우선순위 선택: 연도(가장 최근)→비교집단 일치도→분위 완전성.

    특정 지표가 없다면 범용 업종/규모 기준을 탐색하되 원본 요청값과 실제값을
    별도로 명시한다. 기본 비교는 항상 reference_only.
    """
    if company_size not in ('A', 'L', 'M'):
        raise ValueError('company_size는 A/L/M')
    if not isinstance(max_reference_gap, int) or max_reference_gap < 0:
        raise ValueError('max_reference_gap은 0 이상 정수')
    if ratios_result is None:
        ratios_result = calculate_financial_ratios(data)
    if (ratios_result.get('corp_code') != data['corp_code']
            or ratios_result.get('fs_div') != data['fs_div']
            or set(ratios_result.get('years', {})) != set(data.get('financials', {}))):
        raise ValueError('계산된 재무비율 기업/재무제표/연도가 입력과 다름')
    if not data['financials']:
        raise ValueError('재무제표 데이터가 없습니다')
    if year is None:
        year = max(data['financials'], key=lambda k: data['financials'][k]['period_end'])
    if year not in data['financials']:
        raise ValueError(f'요청 사업연도 미확보: {year}')
    annual = data['financials'][year]
    target_year = int(annual['fiscal_year'])
    ec_code = industry_code if industry_code is not None else data.get('industry_code')
    indexed = {(r['industry_code'], r['fiscal_year'], r['company_size'], r['metric']): r
               for r in benchmarks}
    out = {}
    for metric, direction in METRIC_DIRECTION.items():
        if metric in {"interest_coverage_ratio", "borrowing_dependency_ratio"}:
            continue  # 범용 MVP에서는 원천 데이터 신뢰성 문제로 제외
        observed = ratios_result['years'][year]['ratios'].get(metric, {})
        value = observed.get('value')
        item = {'label': RATIO_LABELS.get(metric, metric), 'value': value, 'unit': '%',
                'status': 'not_assessed', 'reason': None,
                'comparison_type': None, 'quantile_coverage': 'not_assessed',
                'original_industry_code': ec_code, 'requested_company_size': company_size,
                'fallback_used': False}
        if value is None:
            item['reason'] = observed.get('reason') or '원천 계정 또는 전기 값 부족'
        elif not ec_code:
            item['reason'] = 'ECOS 한국표준산업분류 코드 미확보'
        elif not math.isfinite(float(value)):
            item['reason'] = '기업 재무비율이 유효한 숫자가 아님'
        else:
            b = None
            fallback = None
            # 최신 연도 데이터 우선. 같은 연도에서는 비교집단의 정확한 일치 우선.
            # 과거년도의 exact보다 최신년도 broad를 택할지는 정책이므로 여기서는
            # exact 우선(동일회계연도 확보 여부는 결과에 표시)으로 설정.
            for ind, size, path_type in _candidate_paths(ec_code, company_size, allow_fallback):
                candidates = [r for (code, by, csize, m), r in indexed.items()
                              if code == ind and csize == size and m == metric
                              and 0 <= target_year - by <= max_reference_gap]
                if candidates:
                    b = max(candidates, key=lambda r: r['fiscal_year'])
                    fallback = path_type
                    break
            if b is None:
                item['reason'] = '비교 가능한 동일/대체 업종·기업규모 기준 없음'
            else:
                by = b['fiscal_year']
                quantile_level = b.get('quantile_coverage') or ('full_quantile'
                    if all(b.get(q) is not None for q in ('p25','p50','p75')) else 'median_only')
                relative = 'at_median'
                if value != b['p50'] and direction != 'contextual':
                    better = (value < b['p50']) if direction == 'low' else (value > b['p50'])
                    relative = 'favorable_side' if better else 'unfavorable_side'
                elif direction == 'contextual':
                    relative = 'context_required'
                used_fallback = fallback != 'exact'
                reason = ('한국은행 모집단/회계기준/산식과 DART의 차이 가능: 참고 비교'
                          + ('; 동일 집단 기준 없음으로 대체 집단 사용' if used_fallback else '')
                          + ('; 과거 연도 기준' if by < target_year else '')
                          + ('; p25/p75 미확보로 중앙값만 비교' if quantile_level == 'median_only' else ''))
                item.update({
                    'status': 'reference_only', 'comparison_type': 'reference_only',
                    'comparison_year_type': 'same_year' if by == target_year else 'historical_reference',
                    'benchmark_year': by, 'quantile_coverage': quantile_level,
                    'percentile_band': _band(value, b),
                    'relative_to_median': relative, 'median': b['p50'],
                    'difference_from_median_pp': round(value - b['p50'], 2),
                    'quantiles': {q: b.get(q) for q in ('p25', 'p50', 'p75')},
                    'source': b['source_type'], 'source_url': b['source_url'],
                    'benchmark_version': b['benchmark_version'],
                    'industry_name': b['industry_name'],
                    'benchmark_industry_code': b['industry_code'],
                    'benchmark_company_size': b['company_size'],
                    'company_size': b['company_size'],
                    'fallback_used': used_fallback, 'fallback_type': fallback,
                    'reason': reason,
                })
        out[metric] = item
    return {
        'corp_code': data['corp_code'], 'corp_name': data['corp_name'],
        'year_key': year, 'fiscal_year': target_year, 'period_end': annual['period_end'],
        'industry_code': ec_code, 'company_size': company_size, 'fs_div': data['fs_div'],
        'metrics': out,
        'coverage': {
            'compared': sum(v['status'] == 'reference_only' for v in out.values()),
            'total': len(out),
            'full_quantile': sum(v['quantile_coverage'] == 'full_quantile' for v in out.values()),
            'median_only': sum(v['quantile_coverage'] == 'median_only' for v in out.values()),
            'fallback_used': sum(v['fallback_used'] for v in out.values()),
        },
        'note': 'ECOS 업종통계는 상대적 위치 참고용이며 위험·안전 판정이 아니다. 대체 기준/산식/연도/회계범위 확인 필요.',
    }


def compare_with_latest_bok(data: FinancialData, *, company_size='A',
                            industry_code: str | None = None, max_reference_gap=1,
                            cache_dir=None, refresh=False, allow_fallback=True) -> dict[str, Any]:
    """최신 기업 연도 이하 통계를 읽고, A/L/M 모두 로드하여 업종/규모 fallback.

    동일 연도 자료가 있으면 그 연도를 쓰고, 이전 연도는 필요 시만 다운로드.
    수집 오류(EcosAPIError 등)는 무시하지 않음. 로컬 파일/감사보고서 같이 반환.
    """
    from benchmarks.bok_client import get_benchmarks, audit_path, cache_path
    if not data.get('financials'):
        raise ValueError('재무제표 데이터가 없습니다')
    target_year = max(a['fiscal_year'] for a in data['financials'].values())
    collected = []
    reports = []
    for candidate in range(target_year, target_year - max_reference_gap - 1, -1):
        try:
            _, path = get_benchmarks(year=candidate, company_size='A',
                                     cache_dir=cache_dir, refresh=refresh)
        except (LookupError, ValueError) as exc:
            # 명시적 업종/연도 데이터 부재는 다음 기준 연도를 시도하되
            # 인증/네트워크/중복/비정상 프로세스 오류를 은폐하지 않는다.
            if not isinstance(exc, LookupError) and '중앙값 사용 가능 비율' not in str(exc) and '업종 수 부족' not in str(exc):
                raise
            continue
        report_file = audit_path(candidate, path.parent)
        if report_file.exists():
            reports.append(json.loads(report_file.read_text(encoding='utf-8')))
        for size in ('A', 'L', 'M'):
            f = cache_path(candidate, size, path.parent)
            if f.exists():
                # 이전 실행의 다른 버전 파일을 뒤섞지 않도록 감사보고서의 기록을 확인.
                if reports and size not in reports[-1].get('saved_company_sizes', []):
                    continue
                collected.extend(load_benchmarks(f))
    result = compare_industry(data, collected, company_size=company_size,
                              industry_code=industry_code, max_reference_gap=max_reference_gap,
                              allow_fallback=allow_fallback)
    if reports:
        newest = max(reports, key=lambda r: r['fiscal_year'])
        ind = industry_code or data.get('industry_code')
        size_info = newest.get('size_coverage', {}).get(company_size, {})
        result['benchmark_data_quality'] = {
            'status': 'partial' if newest.get('group_coverage', 0) < 1 else 'complete',
            'expected_metric_groups': newest.get('expected_metric_groups'),
            'complete_metric_groups': newest.get('complete_metric_groups'),
            'usable_median_groups': newest.get('usable_median_groups'),
            'group_coverage': newest.get('group_coverage'),
            'usable_median_coverage': newest.get('usable_median_coverage'),
            'selected_size_coverage': size_info,
            'target_industry_coverage': newest.get('industry_coverage', {}).get(ind),
            'metric_coverage': newest.get('metric_coverage'),
            'industry_source': newest.get('industry_source'),
            'audit_file': str(audit_path(newest['fiscal_year'], cache_dir if cache_dir is not None else None)),
            'benchmark_years_loaded': sorted({r['fiscal_year'] for r in reports}, reverse=True),
        }
    return result
