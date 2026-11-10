"""기업 검색: OpenDART corpCode.xml 목록 중 사업보고서 제출 기업 + 테스트용 시드 5개.

v3.1 변경
- 예외 메시지에서 인증키를 가린다(기존에는 연결 오류 문자열에 crtfc_key가 그대로 노출됨).
- 검색 순위: 완전일치 → 접두일치 → 상장사 → 이름 짧은 순. 부분일치 기업이 많을 때
  가나다순 정렬로 상장 대표기업이 12개 밖으로 밀려나는 문제를 막는다.

v3.2 변경
- corpCode.xml에는 공시 고유번호를 가진 모든 법인(약 12만 개)이 들어 있지만, 분석은 사업보고서
  재무제표로만 가능하다. 공시검색 API(list.json, A001 사업보고서)로 최근 N개월 안에 사업보고서를
  제출한 기업만 골라 목록에 남기고, 최근 제출일(last_annual_report) 열을 추가한다.
- 이 열이 없는 예전 목록 파일은 catalog_is_current()가 False를 돌려 앱이 다시 만들게 한다.

v3.7 변경: 국내 비금융 상장기업으로 한정
- 상장 여부: 종목코드가 있는 회사만 기업개황(company.json)을 조회해, 법인구분(corp_cls)이
  유가증권(Y)·코스닥(K)·코넥스(N)인 회사만 남긴다(상장폐지 등은 E). 기업개황은 data/company_info에 저장해 재사용.
- 외국 기업: 종목코드가 9로 시작하면 제외(거래소가 외국 기업에 9로 시작하는 코드를 부여).
- 금융업: 업종코드(KSIC) 64·65·66 제외. 단 지주회사(64992)는 비금융 지주(LG·SK 등)를 살리기 위해 포함하고,
  이름에 '금융'이 있거나 예외 목록(신한지주 등)이면 금융지주로 보아 제외.
- 스팩('스팩'·'기업인수목적')·리츠('리츠'·'부동산투자회사')는 이름으로 제외(업종코드로 안 걸리는 경우 대비).
- 제외한 회사와 사유는 data/companies_excluded.csv에 남긴다.
"""
from __future__ import annotations
import csv
import io
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
import requests
from .client import dart_key, ROOT, DartAPIError, mask_secret, get_json, company_info

CATALOG_FIELDS = ['corp_code', 'corp_name', 'stock_code', 'modify_date', 'last_annual_report',
                  'corp_cls', 'induty_code']
EXCLUDED_FIELDS = ['corp_code', 'corp_name', 'stock_code', 'corp_cls', 'induty_code', 'reason']
LISTED_CLASSES = {'Y': '유가증권', 'K': '코스닥', 'N': '코넥스'}
FINANCIAL_KSIC_PREFIXES = ('64', '65', '66')
HOLDING_COMPANY_KSIC = '64992'
# 이름에 '금융'이 없는 금융지주. 새로 확인되면 여기에 추가한다.
FINANCIAL_HOLDING_EXCEPTIONS = {'신한지주'}
SPAC_KEYWORDS = ('스팩', '기업인수목적')


def is_reit(name: str, induty: str) -> bool:
    """리츠 판별. '리츠'가 이름 중간에 들어간 일반 회사(메리츠금융지주, 블리츠웨이엔터테인먼트)를 잘못 빼지 않도록
    이름이 '리츠'로 끝나거나 '부동산투자회사'를 포함하거나, '리츠'를 포함하면서 부동산업(68)일 때만 리츠로 본다."""
    return name.endswith('리츠') or '부동산투자회사' in name or ('리츠' in name and induty.startswith('68'))


def classify_company(row: dict, info: dict | None) -> str | None:
    """국내 비금융 상장기업이면 None, 아니면 제외 사유. info는 기업개황(company.json) 응답."""
    stock = str(row.get('stock_code') or '').strip()
    name = str(row.get('corp_name') or '')
    if not stock:
        return '비상장'
    if stock.startswith('9'):
        return '외국 기업(종목코드 9xxxxx)'
    corp_cls = str((info or {}).get('corp_cls') or '').strip()
    if corp_cls not in LISTED_CLASSES:
        return f'현재 상장 아님(법인구분 {corp_cls or "확인 불가"})'
    induty = str((info or {}).get('induty_code') or '').strip()
    if any(k in name for k in SPAC_KEYWORDS):
        return '스팩(기업인수목적회사)'
    if is_reit(name, induty):
        return '리츠(부동산투자회사)'
    if induty.startswith(FINANCIAL_KSIC_PREFIXES):
        if induty.startswith(HOLDING_COMPANY_KSIC):
            if '금융' in name or name in FINANCIAL_HOLDING_EXCEPTIONS:
                return f'금융지주(업종 {induty})'
            return None  # 비금융 지주회사는 분석 대상
        return f'금융·보험업(업종 {induty})'
    return None
ANNUAL_REPORT_MONTHS = 24     # 12월 외 결산·제출 지연 기업까지 포함하도록 2년
LIST_WINDOW_DAYS = 90         # 공시검색은 corp_code 없이 조회하면 검색기간이 3개월로 제한됨
LIST_PAGE_COUNT = 100         # list.json 한 페이지 최대 건수

SEEDS = [
    {'corp_name':'링네트','stock_code':'042500','corp_code':'00352499'},
    {'corp_name':'롯데이노베이트','stock_code':'286940','corp_code':'01262032'},
    {'corp_name':'에스트래픽','stock_code':'234300','corp_code':'01014055'},
    {'corp_name':'이삭엔지니어링','stock_code':'351330','corp_code':'01135640'},
    {'corp_name':'인성정보','stock_code':'033230','corp_code':'00229021'},
]
_KEYS = ('corp_name', 'stock_code', 'corp_code')


def _norm(value) -> str:
    return ''.join(str(value or '').split()).casefold()


def _rank(row: dict, q: str) -> tuple:
    fields = [_norm(row.get(k)) for k in _KEYS]
    exact = any(f == q for f in fields)
    prefix = any(f.startswith(q) for f in fields)
    listed = bool(str(row.get('stock_code') or '').strip())
    name = str(row.get('corp_name', ''))
    return (not exact, not prefix, not listed, len(name), name)


def find_companies(query: str, catalog: list[dict] | None = None, limit=15) -> list[dict]:
    q = _norm(query)
    if not q:
        return []
    catalog = catalog if catalog is not None else SEEDS
    matches = [row for row in catalog if any(q in _norm(row.get(k)) for k in _KEYS)]
    matches.sort(key=lambda row: _rank(row, q))
    return matches[:limit]


def load_catalog(path: str | Path | None = None) -> list[dict]:
    file = Path(path or ROOT/'data'/'companies.csv')
    if not file.exists():
        return list(SEEDS)
    with file.open('r',encoding='utf-8-sig',newline='') as fh:
        return list(csv.DictReader(fh))


def catalog_is_current(path: str | Path | None = None) -> bool:
    """사업보고서 제출 기업으로 걸러 만든(v3.2 형식) 목록 파일인지 확인."""
    file = Path(path or ROOT/'data'/'companies.csv')
    if not file.exists():
        return False
    try:
        with file.open('r', encoding='utf-8-sig', newline='') as fh:
            header = next(csv.reader(fh), [])
    except OSError:
        return False
    return {'last_annual_report', 'corp_cls', 'induty_code'} <= set(header)


def _date_windows(start: date, end: date, days: int = LIST_WINDOW_DAYS) -> list[tuple[date, date]]:
    """[start, end]를 겹치지 않는 days일 이하 구간으로 나눈다."""
    windows = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return windows


def fetch_annual_report_filers(months: int = ANNUAL_REPORT_MONTHS, *, today: date | None = None,
                               fetch=None, pause: float = 0.1) -> dict[str, str]:
    """최근 months개월 안에 사업보고서(A001, 정정 포함)를 낸 기업 → 가장 최근 접수일(YYYYMMDD).

    공시검색 API는 corp_code 없이 조회하면 3개월 이내 기간만 허용하므로 구간을 나눠 페이지 끝까지 받는다.
    통신·인증·한도 오류는 DartAPIError로 그대로 올린다(일부만 받은 목록으로 저장하지 않기 위함).
    """
    fetch = fetch or get_json
    end = today or date.today()
    start = end - timedelta(days=int(months * 365.25 / 12))
    filers: dict[str, str] = {}
    for bgn, stop in _date_windows(start, end):
        page = 1
        while True:
            result = fetch('list', {
                'bgn_de': bgn.strftime('%Y%m%d'), 'end_de': stop.strftime('%Y%m%d'),
                # 주의: pblntf_ty='A'를 함께 보내면 상세유형이 무시되어 반기·분기보고서까지 섞인다(실호출로 확인).
                'pblntf_detail_ty': 'A001',
                'page_no': str(page), 'page_count': str(LIST_PAGE_COUNT),
            })
            for item in result.get('list') or []:
                code = str(item.get('corp_code') or '').strip()
                filed = str(item.get('rcept_dt') or '').strip()
                if code and filed > filers.get(code, ''):
                    filers[code] = filed
            try:
                total_page = int(result.get('total_page') or 0)
            except (TypeError, ValueError):
                total_page = 0
            if page >= total_page:
                break
            page += 1
            if pause:
                time.sleep(pause)
    return filers


def update_full_catalog(path: str | Path | None = None, *, months: int = ANNUAL_REPORT_MONTHS,
                        filers: dict[str, str] | None = None, info_lookup=None,
                        excluded_path: str | Path | None = None, progress=None) -> int:
    """공식 고유번호 목록 ZIP/XML → 사업보고서 제출 기업 → 국내 비금융 상장기업만 저장. 별도 DB 서버 필요 없음.

    filers를 넘기면 공시검색 API를 호출하지 않는다(테스트·재사용용).
    info_lookup(corp_code) → 기업개황. 기본은 data/company_info 저장본을 재사용하는 company_info.
    제외한 회사와 사유는 excluded_path(기본: 목록 파일 옆 companies_excluded.csv)에 남긴다.
    """
    dest = Path(path or ROOT/'data'/'companies.csv')
    try:
        resp = requests.get('https://opendart.fss.or.kr/api/corpCode.xml',params={'crtfc_key':dart_key()},timeout=90)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml = zf.read(next(n for n in zf.namelist() if n.lower().endswith('.xml')))
        root = ET.fromstring(xml)
    except (requests.RequestException, zipfile.BadZipFile, StopIteration,ET.ParseError) as exc:
        # 원 예외 문자열(요청 URL 포함)은 인증키를 담을 수 있으므로 반드시 가린다.
        raise DartAPIError(f'기업 목록 다운로드/압축 해제 실패: {type(exc).__name__}: {mask_secret(exc)}') from None
    all_rows=[]
    for node in root.findall('.//list'):
        row={k:(node.findtext(k) or '').strip() for k in ('corp_code','corp_name','stock_code','modify_date')}
        if row['corp_code'] and row['corp_name']:
            all_rows.append(row)
    if not all_rows:
        raise DartAPIError('기업 목록이 비어 있습니다. 인증키 및 원문 응답 확인 필요')
    if filers is None:
        filers = fetch_annual_report_filers(months)
    filed=[{**row,'last_annual_report':filers[row['corp_code']]} for row in all_rows if row['corp_code'] in filers]
    if not filed:
        # 공시검색 결과가 비었다면 API 이상일 가능성이 크다. 기존 목록을 빈 목록으로 덮어쓰지 않는다.
        raise DartAPIError(f'최근 {months}개월 사업보고서 제출 기업을 찾지 못했습니다. 공시검색 API 응답을 확인하세요.')
    def default_lookup(code):
        cached = (dest.parent / 'company_info' / f'{code}.json').exists()
        info = company_info(code, data_dir=dest.parent)
        if not cached:
            time.sleep(0.05)  # 새로 조회할 때만 짧게 쉬어 OpenDART에 몰아서 요청하지 않는다
        return info

    lookup = info_lookup or default_lookup
    rows, excluded = [], []
    for i, row in enumerate(filed, 1):
        info = None
        # 비상장·외국 기업은 기업개황을 조회하지 않고 바로 제외한다(API 호출 절약).
        stock = row['stock_code'].strip()
        if stock and not stock.startswith('9'):
            info = lookup(row['corp_code'])  # 통신·한도 오류는 그대로 올려 일부만 거른 목록을 저장하지 않는다
        reason = classify_company(row, info)
        extra = {'corp_cls': str((info or {}).get('corp_cls') or ''), 'induty_code': str((info or {}).get('induty_code') or '')}
        if reason:
            excluded.append({**{k: row.get(k, '') for k in ('corp_code', 'corp_name', 'stock_code')}, **extra, 'reason': reason})
        else:
            rows.append({**row, **extra})
        if progress and i % 200 == 0:
            progress(i, len(filed))
    if not rows:
        raise DartAPIError('국내 비금융 상장기업이 하나도 남지 않았습니다. 기업개황 API 응답을 확인하세요.')
    dest.parent.mkdir(parents=True,exist_ok=True)
    _write_csv(dest, CATALOG_FIELDS, rows)
    _write_csv(Path(excluded_path) if excluded_path else dest.with_name('companies_excluded.csv'),
               EXCLUDED_FIELDS, excluded)
    return len(rows)


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description='OpenDART 기업 자동완성용 목록 관리 (국내 비금융 상장기업)')
    parser.add_argument('--update',action='store_true',help='corpCode.xml + 공시검색 + 기업개황으로 목록 갱신')
    parser.add_argument('--months',type=int,default=ANNUAL_REPORT_MONTHS,help='최근 몇 개월 안의 사업보고서 제출 기업을 남길지')
    parser.add_argument('--query',help='기업명/종목코드 부분 검색')
    args=parser.parse_args()
    if args.update:
        count=update_full_catalog(months=args.months,
                                  progress=lambda i,n: print(f'  기업개황 확인 {i:,}/{n:,}',flush=True))
        print(f'국내 비금융 상장기업 {count:,}개로 목록을 갱신했습니다. 제외 내역: data/companies_excluded.csv')
    if args.query:
        for c in find_companies(args.query,load_catalog()):
            print(c['corp_name'],c.get('stock_code'),c['corp_code'])
