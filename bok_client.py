"""한국은행 ECOS 기업경영분석 501Y086: 실데이터 수집·4차원 완전성 검증.

검사 축: (업종, 기업규모 A/L/M, 재무지표 8개, 분위 p25/p50/p75).
공식적으로 미제공인 조합은 0으로 채우지 않으며 상세 보고서에 기록한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

STAT_CODE = '501Y086'
BASE_URL = 'https://ecos.bok.or.kr/api'
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / 'data' / 'benchmarks'
METRIC_CODES = {
    '501': 'total_asset_growth', '502': 'ppe_growth', '506': 'revenue_growth',
    '610': 'net_margin', '611': 'operating_margin', '627': 'interest_coverage_ratio',
    '707': 'debt_ratio', '710': 'borrowing_dependency_ratio',
}
QUARTILES = {'A1000': 'p25', 'A2000': 'p50', 'A3000': 'p75'}
COMPANY_SIZES = ('A', 'L', 'M')
CACHE_SCHEMA_VERSION = 2  # 중앙값 부분사용 및 A/L/M 동시 캐시
QUANTILE_NAMES = ('p25', 'p50', 'p75')
CSV_FIELDS = [
    'industry_code', 'industry_name', 'fiscal_year', 'company_size', 'metric',
    'p25', 'p50', 'p75', 'quantile_coverage', 'source_type', 'stat_code', 'unit', 'accounting_basis',
    'source_url', 'benchmark_version',
]


class EcosAPIError(RuntimeError):
    """인증/통신/응답 오류. '자료 없음'과 구분하기 위한 예외."""


def load_api_key() -> str:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError('python-dotenv 설치 필요: pip install python-dotenv') from exc
    load_dotenv(PROJECT_ROOT / '.env')
    key = os.getenv('ECOS_API_KEY', '').strip()
    if not key:
        raise ValueError('프로젝트 루트 .env에 ECOS_API_KEY가 없습니다')
    return key


def _paged_api(service: str, api_key: str, suffix: str, *, session=None, page_size=1000) -> list[dict]:
    client = session if session is not None else requests.Session()
    start, total = 1, None
    rows = []
    while total is None or start <= total:
        end = start + page_size - 1
        url = f'{BASE_URL}/{service}/{api_key}/json/kr/{start}/{end}/{suffix}'
        try:
            resp = client.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise EcosAPIError(f'ECOS {service} 통신/JSON 오류') from exc
        if service not in data:
            result = data.get('RESULT', {})
            if result.get('CODE') == 'INFO-200':
                return []
            raise EcosAPIError(f"ECOS {service} 오류: {result.get('CODE', 'UNKNOWN')} {result.get('MESSAGE', '')}")
        payload = data[service]
        try:
            total = int(payload['list_total_count'])
            chunk = payload['row']
        except (KeyError, ValueError, TypeError) as exc:
            raise EcosAPIError(f'ECOS {service} 응답 필드 누락') from exc
        if not chunk:
            raise EcosAPIError(f'ECOS {service} 페이지 누락: {start}~{end}')
        rows.extend(chunk)
        start += len(chunk)
    if len(rows) != total:
        raise EcosAPIError(f'ECOS {service} 총건수 불일치: {len(rows)}/{total}')
    return rows


def fetch_rows(year: int, api_key: str, *, session=None, page_size=1000) -> list[dict]:
    """해당 연도 전체 업종·기업규모 데이터를 페이지 끝까지 조회."""
    if not 2013 <= year <= datetime.now().year:
        raise ValueError('조회 연도가 유효하지 않습니다')
    return _paged_api('StatisticSearch', api_key, f'{STAT_CODE}/A/{year}/{year}', session=session, page_size=page_size)


def fetch_expected_industries(api_key: str, *, session=None) -> dict[str, str]:
    """StatisticItemList의 실제 업종 분류(ITEM_CODE)를 받아 업종 통째 누락도 탐지."""
    items = _paged_api('StatisticItemList', api_key, STAT_CODE, session=session)
    industries = {}
    for item in items:
        if item.get('GRP_NAME') == '업종코드':
            code = str(item.get('ITEM_CODE') or '').strip()
            if code:
                industries[code] = str(item.get('ITEM_NAME') or code).strip()
    if not industries:
        raise EcosAPIError('ECOS StatisticItemList에서 업종코드 목록을 읽지 못했습니다')
    return industries


def _parse_number(value: Any) -> Decimal | None:
    if value is None or str(value).strip() in ('', '-', '..', '…'):
        return None
    try:
        parsed = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def audit_coverage(raw_rows: list[dict], year: int, *,
                   expected_industries: dict[str, str] | None = None,
                   company_sizes: tuple[str, ...] = COMPANY_SIZES) -> dict:
    """전 조합을 확인하는 상세 진단. 완전/부분누락/미제공/비정상 값을 구분.

    공식 미제공 조합과 수집 오류는 API 값만으로 단정할 수 없으므로
    missing 상태를 `not_published_or_missing`으로 기록한다.
    """
    metrics = tuple(METRIC_CODES.values())
    names = dict(expected_industries or {})
    cells: dict[tuple[str, str, str, str], Decimal | None] = {}
    duplicates, invalid_values, unknown_codes = [], [], []
    relevant_count = 0
    for r in raw_rows:
        if str(r.get('TIME')) != str(year):
            continue
        ind = str(r.get('ITEM_CODE1') or '').strip()
        size = str(r.get('ITEM_CODE2') or '').strip()
        metric = METRIC_CODES.get(str(r.get('ITEM_CODE3') or ''))
        quartile = QUARTILES.get(str(r.get('ITEM_CODE4') or ''))
        if not ind or size not in COMPANY_SIZES or not metric or not quartile:
            continue
        relevant_count += 1
        if expected_industries is not None and ind not in expected_industries:
            unknown_codes.append(ind)
        names.setdefault(ind, str(r.get('ITEM_NAME1') or ind).strip())
        key = (ind, size, metric, quartile)
        if key in cells:
            duplicates.append({'industry_code': ind, 'company_size': size, 'metric': metric, 'quartile': quartile})
            continue
        value = _parse_number(r.get('DATA_VALUE'))
        cells[key] = value
        if value is None:
            invalid_values.append({'industry_code': ind, 'company_size': size, 'metric': metric, 'quartile': quartile, 'raw_value': r.get('DATA_VALUE')})

    inspected = sorted(names)
    details, complete_keys, usable_keys = [], set(), set()
    counts, by_size, by_metric, by_industry = (defaultdict(int) for _ in range(4))
    issues_by_size = defaultdict(Counter)
    observed_by_size = Counter()
    for ind in inspected:
        for size in company_sizes:
            for metric in metrics:
                prefix = (ind, size, metric)
                values = {q: cells.get((*prefix, q)) for q in QUANTILE_NAMES}
                observed = [q for q in QUANTILE_NAMES if (*prefix, q) in cells]
                valid = [q for q, v in values.items() if v is not None]
                missing = [q for q, v in values.items() if v is None]
                if observed:
                    observed_by_size[size] += 1
                # 부분값 간 순서도 검증한다. p50만 있으면 중앙값 단독 사용 가능.
                ordered = all(values[a] <= values[b] for a, b in zip(valid, valid[1:]))
                if not observed:
                    status = 'not_published_or_missing'
                elif not ordered:
                    status = 'invalid_order'
                elif not missing:
                    status = 'complete'
                    complete_keys.add(prefix)
                    usable_keys.add(prefix)
                elif values['p50'] is not None:
                    status = 'median_only'
                    usable_keys.add(prefix)
                else:
                    status = 'incomplete'
                counts[status] += 1
                by_size[size, status] += 1
                by_metric[metric, status] += 1
                by_industry[ind, status] += 1
                issues_by_size[size][status] += 1
                if status != 'complete':
                    details.append({
                        'industry_code': ind, 'industry_name': names[ind],
                        'company_size': size, 'metric': metric, 'status': status,
                        'missing_quartiles': missing,
                        # 존재 여부와 유효한 숫자 여부를 명확히 분리.
                        'observed_quartiles': observed,
                        'valid_quartiles': valid,
                        'present_quartiles': observed,  # 구버전 호환
                    })
    total = len(inspected) * len(company_sizes) * len(metrics)
    denominator = len(inspected) * len(metrics)
    def summary(counted):
        full = counted.get('complete', 0)
        med = counted.get('median_only', 0)
        return {
            'total_groups': denominator,
            'full_quantile_groups': full,
            'median_only_groups': med,
            'usable_groups': full + med,
            'full_quantile_coverage': round(full / denominator, 4) if denominator else 0,
            'usable_median_coverage': round((full + med) / denominator, 4) if denominator else 0,
            'status_counts': dict(counted),
        }
    size_details = {size: summary(issues_by_size[size]) for size in company_sizes}
    metric_details = {}
    for metric in metrics:
        metric_details[metric] = {size: {'complete': sum(1 for key in complete_keys if key[1] == size and key[2] == metric),
                                         'median_only': sum(1 for key in usable_keys - complete_keys if key[1] == size and key[2] == metric),
                                         'total': len(inspected)} for size in company_sizes}
    industry_details = {ind: {size: {'complete': sum(1 for key in complete_keys if key[:2] == (ind, size)),
                                     'median_only': sum(1 for key in usable_keys - complete_keys if key[:2] == (ind, size)),
                                     'total': len(metrics)} for size in company_sizes} for ind in inspected}
    return {
        'cache_schema_version': CACHE_SCHEMA_VERSION,
        'stat_code': STAT_CODE, 'fiscal_year': year,
        'industry_source': 'StatisticItemList' if expected_industries is not None else 'observed_rows_only',
        'industry_count': len(inspected), 'expected_industry_codes': inspected,
        'company_sizes': list(company_sizes), 'metrics': list(metrics),
        'quartiles': list(QUANTILE_NAMES), 'raw_relevant_observations': relevant_count,
        'raw_observation_count': len(raw_rows),
        'expected_metric_groups': total,
        'complete_metric_groups': len(complete_keys),
        'usable_median_groups': len(usable_keys),
        'group_coverage': round(len(complete_keys) / total, 4) if total else 0,
        'usable_median_coverage': round(len(usable_keys) / total, 4) if total else 0,
        'status_counts': dict(counts), 'duplicate_cells': duplicates,
        'invalid_value_cells': invalid_values,
        'unknown_industry_codes': sorted(set(unknown_codes)),
        'size_coverage': size_details,
        'metric_coverage': metric_details,
        'industry_coverage': industry_details,
        'incomplete_groups': details,
        'source_reconciliation': {
            'relevant_observations': relevant_count,
            'expected_observation_cells': total * 3,
            'observed_unique_cells': len(cells),
            'unknown_missing_origin': '공식 미제공과 수집 누락은 API만으로 확정 불가',
        },
        'policy_note': '사분위수 결측값 추정/보간/0 대체 금지. p50이 있으면 중앙값만 사용.',
    }


def normalize(raw_rows: list[dict], year: int, *, company_size='A',
              allow_median_only=False) -> list[dict]:
    """유효한 p50을 중심으로 저장한다. 구버전 기본값은 완전 분위수만 저장.

    allow_median_only=True면 p50이 유효한 부분 사분위수도 CSV에 저장한다.
    부분 분위수 사이의 순서 오류는 무조건 제외(값을 추정하지 않음).
    """
    if company_size not in COMPANY_SIZES:
        raise ValueError('company_size는 A/L/M')
    grouped: dict[tuple[str, str], dict[str, Decimal | None]] = defaultdict(dict)
    names = {}
    for r in raw_rows:
        if str(r.get('TIME')) != str(year) or str(r.get('ITEM_CODE2')) != company_size:
            continue
        ind = str(r.get('ITEM_CODE1') or '').strip()
        metric = METRIC_CODES.get(str(r.get('ITEM_CODE3') or ''))
        q = QUARTILES.get(str(r.get('ITEM_CODE4') or ''))
        if not ind or not metric or not q:
            continue
        names[ind] = str(r.get('ITEM_NAME1') or ind).strip()
        key = (ind, metric)
        if q in grouped[key]:
            raise ValueError(f'중복 ECOS 관측치: {key} / {company_size} / {q}')
        grouped[key][q] = _parse_number(r.get('DATA_VALUE'))
    out = []
    for (ind, metric), qs in sorted(grouped.items()):
        valid = [qs[q] for q in QUANTILE_NAMES if qs.get(q) is not None]
        if any(a > b for a, b in zip(valid, valid[1:])):
            raise ValueError(f'ECOS 사분위수 순서 오류: {ind} / {company_size} / {metric}')
        coverage = 'full_quantile' if all(qs.get(q) is not None for q in QUANTILE_NAMES) else 'median_only'
        if qs.get('p50') is None or (coverage != 'full_quantile' and not allow_median_only):
            continue
        out.append({
            'industry_code': ind, 'industry_name': names[ind], 'fiscal_year': year,
            'company_size': company_size, 'metric': metric,
            **{q: str(qs[q]) if qs.get(q) is not None else '' for q in QUANTILE_NAMES},
            'quantile_coverage': coverage,
            'source_type': 'BOK_ECOS', 'stat_code': STAT_CODE, 'unit': '%',
            'accounting_basis': 'BOK_ENTERPRISE_SURVEY',
            'source_url': 'https://ecos.bok.or.kr/',
            'benchmark_version': f'BOK_{STAT_CODE}_{year}_{company_size}',
        })
    return out


def validate_coverage(rows: list[dict], *, min_industries=10) -> None:
    """하위호환: 정규화 결과의 최소 범위 검사. 정밀검사는 audit_coverage 사용."""
    if len({r['industry_code'] for r in rows}) < min_industries:
        raise ValueError(f'데이터 불완전: 업종 수 부족(<{min_industries})')


def cache_path(year: int, company_size='A', cache_dir=None) -> Path:
    folder = Path(cache_dir) if cache_dir is not None else DATA_DIR
    return folder / f'bok_{year}_{company_size}.csv'


def audit_path(year: int, cache_dir=None) -> Path:
    folder = Path(cache_dir) if cache_dir is not None else DATA_DIR
    return folder / f'bok_{year}_coverage.json'


def save_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        raise ValueError('저장할 정상 사분위수 데이터가 없습니다')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    try:
        with tmp.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def save_audit(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def get_benchmarks(*, year=None, company_size='A', refresh=False, max_lookback=5,
                   cache_dir=None, api_key=None, fetcher=None,
                   industry_fetcher=None, min_industries=10,
                   strict_coverage=False, min_size_coverage=0.60) -> tuple[int, Path]:
    """최신 연도 탐색, 공식 업종 목록 및 전체 A/L/M 감사, 규모별 CSV 동시 저장.

    결측 분위수는 공란 저장(p50 유효 시만). 통계 전체가 유효한지 가정하지 않는다.
    중복/미등록 업종은 수집 중단. 기준 완전성은 선택 규모의 p50 가용률.
    """
    if company_size not in COMPANY_SIZES:
        raise ValueError('기업규모는 A/L/M 중 선택하세요')
    if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
        raise ValueError('year는 정수 또는 None')
    if not 0 <= max_lookback <= 30 or not 0 <= min_size_coverage <= 1:
        raise ValueError('조회 기간 또는 가용성 설정 오류')
    current = datetime.now().year
    upper = current if year is None else year
    candidates = [upper] if year is not None else list(range(upper, max(2012, upper-max_lookback-1), -1))
    raw_fetch = fetcher or fetch_rows
    for candidate in candidates:
        path = cache_path(candidate, company_size, cache_dir)
        report_file = audit_path(candidate, cache_dir)
        if path.exists() and report_file.exists() and not refresh:
            report = json.loads(report_file.read_text(encoding='utf-8'))
            if (report.get('cache_schema_version') == CACHE_SCHEMA_VERSION
                    and report.get('stat_code') == STAT_CODE
                    and report.get('fiscal_year') == candidate
                    and report.get('selected_company_size') == company_size
                    and (not strict_coverage or report.get('complete_metric_groups') == report.get('expected_metric_groups'))
                    and report.get('selected_size_usable_coverage', 0) >= min_size_coverage):
                return candidate, path
        key = api_key or load_api_key()
        raw = raw_fetch(candidate, key)
        if not raw:
            continue
        if industry_fetcher is None:
            industries = None if fetcher is not None else fetch_expected_industries(key)
        else:
            industries = industry_fetcher(key)
        report = audit_coverage(raw, candidate, expected_industries=industries)
        if report['duplicate_cells'] or report['unknown_industry_codes']:
            raise ValueError(f'{candidate} 중복/미등록 업종 발견: 감사 필요')
        if strict_coverage and report['complete_metric_groups'] != report['expected_metric_groups']:
            if year is not None:
                raise ValueError(f'{candidate} 전체 조합 불완전: {report["complete_metric_groups"]}/{report["expected_metric_groups"]}')
            continue
        # 비교 중 규모 대체가 가능하도록 한번의 API 수집에서 세 규모 CSV 동시 생성.
        all_rows = {size: normalize(raw, candidate, company_size=size, allow_median_only=True)
                    for size in COMPANY_SIZES}
        expected = report['industry_count'] * len(METRIC_CODES)
        chosen = all_rows[company_size]
        size_coverage = len(chosen) / expected if expected else 0
        if size_coverage < min_size_coverage:
            if year is not None:
                raise ValueError(f'{candidate} {company_size} 중앙값 사용 가능 비율 {size_coverage:.1%} < {min_size_coverage:.1%}')
            continue
        try:
            validate_coverage(chosen, min_industries=min_industries)
        except ValueError:
            if year is not None:
                raise
            continue
        report['selected_company_size'] = company_size
        report['selected_size_normalized_rows'] = len(chosen)
        report['selected_size_usable_coverage'] = round(size_coverage, 4)
        report['selected_size_coverage'] = report['size_coverage'][company_size]['full_quantile_coverage']
        report['saved_company_sizes'] = [size for size, rows in all_rows.items() if rows]
        for size, rows in all_rows.items():
            if rows:
                save_csv(rows, cache_path(candidate, size, cache_dir))
        save_audit(report, report_file)
        return candidate, path
    raise LookupError('조회 범위에 사용 가능한 ECOS 기준 연도가 없습니다')


def main() -> None:
    parser = argparse.ArgumentParser(description='한국은행 ECOS 501Y086 자동 수집 및 4차원 완전성 검사')
    parser.add_argument('--year', type=int, default=None, help='생략하면 최신 데이터 검색')
    parser.add_argument('--company-size', choices=COMPANY_SIZES, default='A')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--strict-coverage', action='store_true', help='모든 업종/규모/지표/분위 누락 시 중단')
    args = parser.parse_args()
    year, path = get_benchmarks(year=args.year, company_size=args.company_size,
                                refresh=args.refresh, strict_coverage=args.strict_coverage)
    report = json.loads(audit_path(year, path.parent).read_text(encoding='utf-8'))
    print(f'검증된 기준 연도: {year} / 데이터: {path}')
    print(f'검증 보고서: {audit_path(year, path.parent)}')
    print(f'전체 완전 사분위수: {report["group_coverage"]:.1%} / 중앙값 사용가능: {report["usable_median_coverage"]:.1%}')
    print(f'{args.company_size} 규모 완전 사분위수: {report["selected_size_coverage"]:.1%} / 중앙값 사용가능: {report["selected_size_usable_coverage"]:.1%}')
    print('미제공/비정상은 추정하지 않으며 검증 보고서에 기록합니다.')


if __name__ == '__main__':
    main()
