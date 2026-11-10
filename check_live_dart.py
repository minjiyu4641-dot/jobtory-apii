"""DART API 실호출 검증 CLI.

예) python -m scripts.check_live_dart --company 링네트 --refresh
    python -m scripts.check_live_dart --company 005930          (전체 목록이 있으면 모든 공시기업 가능)
    python -m scripts.check_live_dart --corp-code 00126380
v3.1: 테스트 5개 기업에 한정하지 않고 data/companies.csv 전체 목록을 사용한다.
"""
from __future__ import annotations
import argparse
from pathlib import Path
from dart.search import SEEDS, find_companies, load_catalog
from dart.pipeline import download_company_five_years
from app_modules.engine import analyze, save_analysis_result

ROOT=Path(__file__).resolve().parents[1]

def inspect_one(company: dict, *, refresh: bool=True, fs_div: str='auto'):
    print(f'\n=== {company["corp_name"]} ({company.get("stock_code") or "비상장"}) DART 실수집 ===',flush=True)
    data,meta=download_company_five_years(company,statement_choice=fs_div,refresh=refresh,data_dir=ROOT/'data')
    report=analyze(data)
    file=save_analysis_result(data,report,data_dir=ROOT/'data',meta=meta)
    print(f'공시 기준: {data["fs_div"]} / 사업보고서 11011',flush=True)
    print('연도별 결과:',flush=True)
    for year in meta['business_year_window']:
        status=meta['year_status'][str(year)]
        print(f'  {year}: {status["status"]}, 핵심 계정 확보 {status.get("nonmissing_accounts","—")}, 접수번호 {status.get("rcept_no") or "—"}',flush=True)
    if meta.get('consistency_checks'):
        print('전기금액 불일치(재작성 가능성):',flush=True)
        for c in meta['consistency_checks']:
            print(f'  {c["year"]} {c["field"]}: {c["original_value"]:,} → {c["restated_value"]:,} ({c["difference_pct"]:+.2f}%)',flush=True)
    print('최근 5개년 정량 분석 저장:',file,flush=True)
    return data,meta,report


def main():
    parser=argparse.ArgumentParser(description='CSV 없이 실제 DART 5개년 수집·정량 분석 검증')
    parser.add_argument('--company',default='링네트',help='기업명 또는 종목코드 (전체 목록 기준)')
    parser.add_argument('--corp-code',help='DART 고유번호 8자리로 직접 지정')
    parser.add_argument('--all',action='store_true',help='테스트 5개 기업 모두 조회 (API 호출 많음)')
    parser.add_argument('--refresh',action='store_true',help='원본 캐시 무시하고 실제 API 재조회')
    parser.add_argument('--fs-div',choices=['auto','CFS','OFS'],default='auto')
    args=parser.parse_args()
    catalog=load_catalog()
    if args.all:
        companies=SEEDS
    elif args.corp_code:
        companies=[r for r in catalog if r['corp_code']==args.corp_code] or [{'corp_code':args.corp_code,'corp_name':args.corp_code,'stock_code':''}]
    else:
        companies=find_companies(args.company,catalog)
        exact=[x for x in companies if args.company in (x['corp_name'],x.get('stock_code'))]
        if len(exact)==1:companies=exact
        elif len(companies)>1:
            for c in companies[:10]:print(' ',c['corp_name'],c.get('stock_code') or '비상장',c['corp_code'])
            parser.error('여러 기업이 검색됨: 정확한 이름이나 --corp-code를 사용하세요.')
    if not companies:parser.error('검색 결과가 없습니다. python -m dart.search --update 로 전체 목록을 먼저 받으세요.')
    failed=[]
    for c in companies:
        try:inspect_one(c,refresh=args.refresh,fs_div=args.fs_div)
        except Exception as e:
            print(f'실패: {c["corp_name"]} - {type(e).__name__}: {e}',flush=True)
            failed.append(c['corp_name'])
    if failed:raise SystemExit('실패 기업: '+', '.join(failed))
    print('\n실제 DART 수집 및 분석 완료. streamlit run app.py 로 확인하세요.')

if __name__=='__main__':main()
