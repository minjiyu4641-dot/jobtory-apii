"""OpenDART 연간 전체 재무제표 수집. 원문 응답 그대로 저장; 실패와 미공시 구분.

v3.1 변경
- 예외 메시지에서 인증키(crtfc_key)를 항상 가린다(mask_secret).
- OpenDART 상태코드별로 사용자에게 의미 있는 메시지를 준다.
- 일시적 네트워크 오류·점검(800)만 짧게 재시도한다. 일일 한도(020)는 재시도하지 않는다.
- '자료 없음(013)' 응답은 캐시 유효기간(NEGATIVE_CACHE_TTL)이 지나면 다시 조회한다.
  공시 전에 받은 013이 영구 저장돼 최신 사업보고서를 놓치는 문제를 막기 위함.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API_BASE = 'https://opendart.fss.or.kr/api'

# '자료 없음' 응답을 신뢰하는 기간(초). 사업보고서가 새로 공시될 수 있으므로 짧게 둔다.
NEGATIVE_CACHE_TTL = 24 * 60 * 60
RETRY_STATUS = {'800'}          # 시스템 점검: 짧게 재시도
MAX_RETRIES = 2
RETRY_BACKOFF_SEC = 1.0

# OpenDART 개발가이드 공통 상태코드 (사용자 안내 문구)
STATUS_MESSAGES = {
    '010': '등록되지 않은 인증키입니다. .env의 DART_API_KEY를 확인해 주세요.',
    '011': '사용할 수 없는 인증키입니다.',
    '012': '접근할 수 없는 IP입니다.',
    '014': '요청한 파일이 존재하지 않습니다.',
    '020': 'OpenDART 일일 요청 한도를 초과했습니다. 내일 다시 시도하거나 캐시된 자료를 사용하세요.',
    '021': '조회 가능한 회사 개수를 초과했습니다.',
    '100': '요청 값이 올바르지 않습니다.',
    '101': '부적절한 접근입니다.',
    '800': 'OpenDART 시스템 점검 중입니다.',
    '900': 'OpenDART 정의되지 않은 오류입니다.',
    '901': '인증키 사용 기간이 만료되었습니다.',
}


class DartAPIError(RuntimeError):
    """OpenDART 통신/인증/한도 오류. '자료 없음(013)'과 구분된다."""

    def __init__(self, message: str, *, status: str | None = None):
        super().__init__(mask_secret(message))
        self.status = status


_SECRET_PATTERNS = (
    re.compile(r'(crtfc_key=)[^&\s\'")]+', re.IGNORECASE),
    re.compile(r'(/api/[A-Za-z]+/)[A-Za-z0-9]{20,}(/json)'),  # ECOS 경로형 키
)


def mask_secret(text: str) -> str:
    """URL·예외 문자열에 섞인 인증키를 가린다. 화면/로그 출력 전에 반드시 통과시킨다."""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(r'\1***', out)
    for env in ('DART_API_KEY', 'ECOS_API_KEY'):
        value = os.getenv(env, '').strip()
        if len(value) >= 8:
            out = out.replace(value, '***')
    return out


def dart_key() -> str:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    key = os.getenv('DART_API_KEY', '').strip()
    if not key:
        raise DartAPIError('프로젝트 최상위 .env에 DART_API_KEY를 넣어주세요.')
    return key


def get_json(endpoint: str, params: dict, *, session=None, sleep=time.sleep) -> dict:
    client = session or requests
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.get(f'{API_BASE}/{endpoint}.json',
                              params={**params, 'crtfc_key': dart_key()}, timeout=20)
            resp.raise_for_status()
            result = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                sleep(RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            raise DartAPIError(f'DART 요청/응답 오류 ({endpoint}): {type(exc).__name__} '
                               '(네트워크 또는 인증키를 확인하세요)') from None
        status = str(result.get('status'))
        if status == '013':
            return {'status': '013', 'message': result.get('message', '조회된 자료 없음'), 'list': []}
        if status == '000':
            return result
        if status in RETRY_STATUS and attempt < MAX_RETRIES:
            sleep(RETRY_BACKOFF_SEC * (attempt + 1))
            continue
        friendly = STATUS_MESSAGES.get(status, result.get('message', ''))
        raise DartAPIError(f'DART 오류 {status}: {friendly} (API={endpoint})', status=status)
    raise DartAPIError(f'DART 요청 실패 ({endpoint}): {type(last_error).__name__}')  # pragma: no cover


COMPANY_INFO_MAX_AGE = 30 * 24 * 60 * 60  # 기업개황(업종·결산월·법인구분)은 자주 바뀌지 않아 30일간 재사용


def company_info(corp_code: str, *, data_dir=None, refresh=False, max_age: float = COMPANY_INFO_MAX_AGE,
                 fetch=None, now: float | None = None) -> dict:
    """기업개황(company.json). data/company_info/{corp_code}.json에 저장해 목록 생성·분석에서 함께 재사용한다.

    정상(000) 응답만 저장하고, 저장본이 max_age보다 오래됐으면 다시 받는다.
    """
    fetch = fetch or get_json
    path = Path(data_dir or ROOT / 'data') / 'company_info' / f'{corp_code}.json'
    if path.exists() and not refresh:
        try:
            age = (now if now is not None else time.time()) - path.stat().st_mtime
            if age < max_age:
                cached = json.loads(path.read_text(encoding='utf-8'))
                if str(cached.get('status')) == '000':
                    return cached
        except (OSError, ValueError):
            pass  # 손상된 저장본은 다시 받는다
    result = fetch('company', {'corp_code': corp_code})
    if str(result.get('status')) == '000':
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    return result


def _cache_is_fresh(path: Path, payload: dict, *, now: float | None = None) -> bool:
    """정상(000) 응답은 계속 사용, 자료 없음(013)은 TTL 동안만 신뢰."""
    if payload.get('status') == '000':
        return True
    age = (now if now is not None else time.time()) - path.stat().st_mtime
    return age < NEGATIVE_CACHE_TTL


def fetch_statement(corp_code: str, business_year: int, fs_div: str, *, refresh=False, data_dir=None) -> dict:
    if fs_div not in ('CFS', 'OFS'):
        raise ValueError('fs_div는 CFS 또는 OFS여야 합니다.')
    root = Path(data_dir or ROOT / 'data') / 'raw' / corp_code
    dest = root / f'{business_year}_{fs_div}_11011.json'
    if dest.exists() and not refresh:
        try:
            cached = json.loads(dest.read_text(encoding='utf-8'))
            if _cache_is_fresh(dest, cached):
                return cached
        except (OSError, ValueError):
            pass  # 손상된 캐시는 다시 받는다
    response = get_json('fnlttSinglAcntAll', {
        'corp_code': corp_code, 'bsns_year': str(business_year),
        'reprt_code': '11011', 'fs_div': fs_div,
    })
    # API 성공과 no data를 분리해 원본·상태를 항상 보존함.
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(f'.{os.getpid()}.tmp')  # 동시 실행 시 임시파일 충돌 방지
    temp.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(dest)
    return response
