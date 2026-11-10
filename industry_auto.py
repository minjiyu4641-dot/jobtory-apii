"""DART 기업개황 업종(한국표준산업분류 숫자 코드) -> ECOS 501Y086 업종.

KSIC 버전이 서로 다를 수 있으므로 일치하는 '두 자리 중분류'가
실제 ECOS 통계의 업종 목록에 있는 경우에만 매칭한다.
"""
from __future__ import annotations
import csv
import json
from pathlib import Path
from typing import Any
from benchmarks.bok_client import DATA_DIR, cache_path, get_benchmarks

# ECOS 501Y086 기업경영분석의 KSIC 대분류/중분류 접두사 (정책 테이블).
# 통계표가 개편되면 메타데이터의 업종표와 재검증이 필요하다.
SECTION_PREFIXES = {
    'C': ('10','11','12','13','14','15','16','17','18','19','20','21','22','23','24','25','26','27','28','29','30','31','32','33','34'),
    'D': ('35',), 'E': ('36','37','38','39'), 'F': ('41','42'),
    'G': ('45','46','47'), 'H': ('49','50','51','52'), 'I': ('55','56'),
    'J': ('58','59','60','61','62','63'), 'L': ('68',),
    'M': ('70','71','72','73'), 'N': ('74','75','76','78','79','80','81'),
    'P': ('85',), 'R': ('90','91'), 'S': ('95','96'),
}


def derive_industry(dart_induty_code: str | None, available_codes: set[str] | None = None) -> dict[str, Any]:
    """매칭 근거를 같이 반환. 불확실하면 업종 없는 상태로 반환."""
    code = ''.join(c for c in str(dart_induty_code or '') if c.isdigit())
    if len(code) < 2:
        return {'industry_code': None, 'method':'unavailable', 'reason':'DART 기업개황에 유효한 업종코드가 없습니다.'}
    prefix = code[:2]
    major = next((group for group, numbers in SECTION_PREFIXES.items() if prefix in numbers), None)
    if not major:
        return {'industry_code': None, 'method':'unavailable', 'reason':f'업종코드 {code}와 ECOS 분류를 확인할 수 없습니다.'}
    # ECOS 분류표에 두 자리 중분류 코드가 존재할 때만 세부업종으로 매칭.
    detailed = f'{major}{prefix}'
    if available_codes is not None and detailed in available_codes:
        return {'industry_code': detailed, 'method':'two_digit_ksic', 'reason':f'DART 업종코드 {code}의 앞 두 자리({prefix})를 ECOS {detailed}와 연결했습니다. 분류 개정 차이 가능성이 있습니다.'}
    if available_codes is None or major in available_codes:
        return {'industry_code': major, 'method':'broad_ksic', 'reason':f'세부업종 {detailed} 기준이 없어 상위 업종 {major}을 사용합니다. 분류 개정 차이 가능성이 있습니다.'}
    return {'industry_code':None,'method':'unavailable','reason':'대응 업종을 ECOS 기준 목록에서 찾지 못했습니다.'}


def _available_codes(cache_dir:Path) -> set[str]:
    for report in sorted(cache_dir.glob('bok_*_coverage.json'),reverse=True):
        try:
            parsed=json.loads(report.read_text(encoding='utf-8'))
            codes=set(parsed.get('expected_industry_codes') or ())
            if codes:return codes
        except (OSError,ValueError):pass
    for csv_path in sorted(cache_dir.glob('bok_*_A.csv'),reverse=True):
        try:
            with csv_path.open(encoding='utf-8-sig',newline='') as handle:
                return {r['industry_code'] for r in csv.DictReader(handle) if r.get('industry_code')}
        except (OSError,ValueError,KeyError):pass
    return set()


def prepare_industry_benchmark(company_info:dict, target_year:int, cache_dir:Path|None=None) -> dict:
    """업종 및 업종비교 자료 자동 준비. 실패는 재무분석 중단 대신 이유를 기록."""
    folder=Path(cache_dir or DATA_DIR)
    info={'industry_code':None,'method':None,'reason':None,'data_year':None,'download_error':None}
    raw_code=company_info.get('induty_code')
    # 업종 자체가 명백히 없는 경우 API 불필요 호출 방지
    trial=derive_industry(raw_code)
    if not trial['industry_code']:
        info.update(trial);return info
    has_data=any(cache_path(y,'A',folder).exists() for y in range(target_year,max(target_year-2,2012)-1,-1))
    if not has_data:
        try:
            # 명시 사업연도 조회; 최신 2025의 보고서와 2026 통계를 섞지 않음.
            for year in range(target_year,max(target_year-2,2012)-1,-1):
                try:
                    get_benchmarks(year=year,company_size='A',cache_dir=folder)
                    break
                except (LookupError, ValueError):
                    # v3.1: 해당 연도 통계가 미공표이거나 커버리지 기준 미달이면 이전 연도를 시도.
                    continue
        except Exception as exc:
            info['download_error']=f'ECOS 기준 자료 자동 수집 실패: {type(exc).__name__}: {exc}'
    available=_available_codes(folder)
    matched=derive_industry(raw_code,available if available else None)
    info.update(matched)
    info['data_year']=max((y for y in range(target_year,max(target_year-2,2012)-1,-1) if cache_path(y,'A',folder).exists()),default=None)
    if not info['data_year'] and not info['download_error']:
        info['download_error']='비교 가능한 ECOS 연간 기준 자료가 아직 확보되지 않았습니다.'
    return info
