"""실제 OpenDART만 수집 -> 원본/표준 12계정/감사기록 -> 분석 입력."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from .client import ROOT, fetch_statement, company_info
from .normalize import normalize_year


def _atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


CHECK_FIELDS=('total_assets','total_equity','revenue','operating_income','net_income','operating_cash_flow')


def consistency_checks(audit: dict, *, tolerance: float=0.005) -> list[dict]:
    """당기 보고서의 '전기' 금액과 직전 연도 보고서의 '당기' 금액을 대조한다.

    차이가 있으면 회계정책 변경·오류수정에 따른 재작성, 사업 중단 재분류,
    결산기 변경 가능성이 있다. 값을 고치지 않고 사실만 기록한다(해석은 화면/Agent 몫).
    """
    out=[]
    keys=sorted(audit, key=lambda k: audit[k]['period_end'])
    for older,newer in zip(keys,keys[1:]):
        for field in CHECK_FIELDS:
            new=audit[newer]['fields'].get(field,{})
            old=audit[older]['fields'].get(field,{})
            restated=new.get('prior_value_in_report')
            original=old.get('value')
            if restated is None or original is None:
                continue
            base=max(abs(original),1)
            if abs(restated-original)/base > tolerance:
                out.append({'field':field,'year':older,'original_value':original,
                            'restated_value':restated,'reported_in':newer,
                            'difference':restated-original,
                            'difference_pct':round((restated-original)/base*100,2)})
    return out


def _build_financial_data(company: dict, by_year: dict[int,list[dict]], *,
                          fs_div: str, settlement_month: int=12,
                          anchor_business_year: int | None=None,
                          data_dir: Path | None=None,
                          validated_overrides: dict | None=None,
                          source: str='DART') -> tuple[dict,dict]:
    """출처 보존 + 전체 5년 창 고정. 원본 없는 연도는 financials에서 빠짐."""
    if not by_year:
        raise ValueError('정규화할 사업보고서 원천 데이터가 없습니다.')
    anchor=int(anchor_business_year or max(by_year))
    years=list(range(anchor-4,anchor+1))
    financials={};audit={};year_status={}
    for year in years:
        rows=by_year.get(year)
        if not rows:
            year_status[str(year)]={'status':'missing','reason':'해당 연도 사업보고서 전체재무제표 미확보'}
            continue
        annual,detail=normalize_year(rows,year,corp_code=company['corp_code'],
             corp_name=company['corp_name'],fs_div=fs_div,settlement_month=settlement_month,
             validated_overrides=(validated_overrides or {}).get(str(year)))
        key=str(annual['fiscal_year'])
        financials[key]=annual
        audit[key]=detail
        year_status[str(year)]={'status':'available','fiscal_year_key':key,
            'nonmissing_accounts':sum(v is not None for v in annual['statement'].values()),
            'rcept_no':detail.get('rcept_no')}
    result={'corp_code':company['corp_code'],'corp_name':company['corp_name'],
      'industry_code':None,'fs_div':fs_div,'report_code':'11011','currency':'KRW',
      'amount_unit':'KRW','financials':financials}
    meta={'source':source,'corp_code':company['corp_code'], 'stock_code':company.get('stock_code',''),
          'fs_div':fs_div,'business_year_window':years,'settlement_month':settlement_month,
          'year_status':year_status,'accounts_audit':audit,
          'consistency_checks':consistency_checks(audit),
          'collected_at':datetime.now().isoformat(timespec='seconds'),
          'warnings':['ECOS 업종코드는 DART 업종코드와 별도 매핑 필요',
                      '정규화 과정에서 누락 계정을 0으로 채우지 않음',
                      '회계연도 변경/비정상 회계기간은 별도 검증 필요']}
    if data_dir is not None:
        root=Path(data_dir)/'normalized'/company['corp_code']/fs_div
        _atomic_json(root/'financial_data.json',result)
        _atomic_json(root/'audit.json',meta)
    return result,meta


def download_company_five_years(company: dict, *, statement_choice='auto',
                                refresh=False, data_dir: Path | None=None,
                                anchor_search_year: int | None=None,
                                validated_overrides: dict | None=None) -> tuple[dict,dict]:
    """가장 최근 공시된 사업연도 탐색 후 해당 연도부터 5년 고정.

    특정 회계연도 CFS 없다고 OFS로 중간 전환하지 않음(비교가능성 우선).
    """
    if statement_choice not in ('auto','CFS','OFS'):
        raise ValueError('statement_choice는 auto/CFS/OFS')
    # 11월 기준 2026년 사업보고서는 아직 없음. 기본 조회 상한은 직전 연도.
    upper=anchor_search_year or datetime.now().year-1
    company=dict(company)
    info=company_info(company['corp_code'])
    company['corp_name']=info.get('corp_name') or company['corp_name']
    settlement=info.get('acc_mt') or '12'
    try: settlement=int(str(settlement).replace('월','').strip())
    except ValueError: settlement=12
    selected=None; latest=None; first_response=None
    options=('CFS','OFS') if statement_choice=='auto' else (statement_choice,)
    for year in range(upper,max(2015,upper-9)-1,-1):
        for kind in options:
            response=fetch_statement(company['corp_code'],year,kind,refresh=refresh,data_dir=data_dir)
            if response.get('status')=='000' and response.get('list'):
                latest=year;selected=kind;first_response=response;break
        if selected:break
    if selected is None:
        raise ValueError('최근 사업보고서에서 조회 가능한 연결/별도 전체재무제표를 찾지 못했습니다.')
    by_year={latest:first_response['list']}
    for year in range(latest-4,latest):
        result=fetch_statement(company['corp_code'],year,selected,refresh=refresh,data_dir=data_dir)
        if result.get('status')=='000' and result.get('list'):
            by_year[year]=result['list']
    output,meta=_build_financial_data(company,by_year,fs_div=selected,
        settlement_month=settlement,anchor_business_year=latest,data_dir=data_dir,
        validated_overrides=validated_overrides,source='DART_API')
    meta['company_info']={'corp_code':info.get('corp_code'), 'corp_name':info.get('corp_name'),
                          'acc_mt':info.get('acc_mt'), 'induty_code':info.get('induty_code')}
    # 최초 수집에 대해 원본은 data/raw/... 폴더에 000/013 모두 존재.
    if data_dir is not None:
        _atomic_json(Path(data_dir)/'normalized'/company['corp_code']/selected/'audit.json',meta)
    return output,meta


def download_separate_for_comparison(company: dict, latest_business_year: int, *, settlement_month: int=12,
                                     refresh=False, data_dir: Path | None=None, years: int=2) -> dict | None:
    """업종 비교 전용 별도재무제표(OFS) 수집.

    한국은행 기업경영분석(ECOS 501Y086)은 개별(별도) 재무제표로 작성되므로, 연결(CFS)로 분석한 회사도
    업종 비교만큼은 같은 기준인 별도재무제표로 계산한다. 성장률에 전년 값이 필요해 최근 2개 연도를 받는다.
    가장 최근 연도의 별도재무제표가 없으면 None(호출 측이 연결 기준 비교로 대신하고 그 사실을 표시).
    """
    by_year={}
    for year in range(latest_business_year-years+1, latest_business_year+1):
        result=fetch_statement(company['corp_code'],year,'OFS',refresh=refresh,data_dir=data_dir)
        if result.get('status')=='000' and result.get('list'):
            by_year[year]=result['list']
    if latest_business_year not in by_year:
        return None
    # 정규화 파일은 저장하지 않는다(data_dir=None). 별도 기준만으로 분석한 회사의 5개년 정규화 파일을
    # 2개년짜리로 덮어쓰지 않기 위함. 원본 응답은 fetch_statement가 data/raw에 이미 저장한다.
    output,meta=_build_financial_data(company,by_year,fs_div='OFS',settlement_month=settlement_month,
                                      anchor_business_year=latest_business_year,data_dir=None,
                                      source='DART_API')
    # 업종 비교 값의 근거(접수번호)를 저장 보고서에 남길 수 있도록 함께 돌려준다. 분석 함수는 이 키를 쓰지 않는다.
    output['sources']={year:{'status':s.get('status'),'rcept_no':s.get('rcept_no')}
                       for year,s in meta['year_status'].items() if s.get('status')=='available'}
    return output
