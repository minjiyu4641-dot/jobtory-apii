"""STEP2~5 연결 (LLM 없이 재현 가능한 정량 결과).

v3.1: 저장 JSON에 스키마 버전·생성시각·출처(접수번호)·데이터 품질 점검을 함께 담아
LangGraph Agent가 이 파일 하나로 근거를 인용할 수 있게 한다.
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from analysis.financial_ratios import calculate_financial_ratios
from analysis.financial_rules import evaluate_financial_rules
from analysis.financial_trends import analyze_financial_trends
from analysis.industry_comparison import compare_industry, load_benchmarks
from benchmarks.bok_client import cache_path


def comparison_source(data: dict, separate: dict | None) -> dict:
    """업종 비교에 쓸 재무제표를 고른다.

    한국은행 기업경영분석은 개별(별도) 재무제표 기준이므로, 같은 결산연도의 별도재무제표가 있으면 그것을 쓴다.
    없으면(별도 미공시·연도 불일치) 분석용 재무제표(연결)를 그대로 쓰고, 호출 측이 기준 차이를 안내한다.
    """
    latest = max(data['financials'].values(), key=lambda a: a['period_end'])
    if separate and separate.get('fs_div') == 'OFS' and separate.get('financials'):
        sep_latest = max(separate['financials'].values(), key=lambda a: a['period_end'])
        if sep_latest['period_end'] == latest['period_end']:
            return separate
    return data


def analyze(data: dict, *, industry_code: str|None=None, company_size='A',
            cache_dir=None, max_reference_gap=1, allow_fallback=True,
            comparison_data: dict | None=None) -> dict:
    """comparison_data: 업종 비교 전용 별도재무제표(OFS). 실적·조건·추세는 data(연결 우선) 기준 그대로."""
    ratios=calculate_financial_ratios(data)
    rules=evaluate_financial_rules(data,ratios)
    trends=analyze_financial_trends(data,ratios,max_years=5)
    benchmarks=[];files=[]
    comp_data=comparison_source(data,comparison_data)
    comp_ratios=ratios if comp_data is data else calculate_financial_ratios(comp_data)
    latest_key=max(comp_data['financials'], key=lambda y:comp_data['financials'][y]['period_end'])
    comparable_calendar=comp_data['financials'][latest_key]['period_end'].endswith('-12-31')
    # 오프라인 대시보드에서 ECOS API 자동호출하지 않음; 사전 다운 받은 파일만 사용.
    if industry_code and comparable_calendar:
        for fy in {a['fiscal_year'] for a in comp_data['financials'].values()}:
            for offset in range(max_reference_gap+1):
                for size in ('A','L','M'):
                    f=cache_path(fy-offset,size,cache_dir)
                    if f.exists() and str(f) not in files:
                        benchmarks+=load_benchmarks(f);files.append(str(f))
    comparison=None
    if industry_code and comparable_calendar and benchmarks:
        comparison=compare_industry(comp_data,benchmarks,ratios_result=comp_ratios,
                 company_size=company_size,industry_code=industry_code,
                 max_reference_gap=max_reference_gap,allow_fallback=allow_fallback)
        comparison['basis_fs_div']=comp_data['fs_div']  # 업종 비교에 실제로 쓴 재무제표 기준
        if comp_data is not data:
            # 업종 비교를 별도재무제표로 계산했다면, 그 금액·비율·접수번호를 함께 남긴다.
            # 저장 보고서(build_report)를 읽는 Agent가 '이 기업' 값의 근거를 인용할 수 있게 하기 위함.
            comparison['basis_data']={
                'fs_div':comp_data['fs_div'],
                'financials':comp_data['financials'],
                'sources':comp_data.get('sources') or {},
                'ratios':{y:{m:r for m,r in v['ratios'].items() if m in comparison['metrics']}
                          for y,v in comp_ratios['years'].items()},
            }
    else:
        note=('비12월 결산 기업은 ECOS 통계연도와 회계기간 일치 검증 전까지 자동 비교 보류'
              if not comparable_calendar else
              'ECOS 산업코드 미선택 또는 해당 연도 로컬 CSV 없음. API 자동호출하지 않음.')
        from app_modules.presentation import SERVICE_BENCHMARK_METRICS
        comparison={'metrics':{},'coverage':{'compared':0,'total':len(SERVICE_BENCHMARK_METRICS)},'note':note,
                    'basis_fs_div':comp_data['fs_div']}
    return {'ratios':ratios,'rules':rules,'trends':trends,'comparison':comparison,
            'benchmark_files':files}


# 1.2: comparison.basis_fs_div(업종 비교 기준)·comparison.basis_data(별도 기준 금액·비율·접수번호) 추가,
#      자본잠식률(capital_impairment_ratio)·rules.years[*].capital_impairment 추가
REPORT_SCHEMA_VERSION='quant-report/1.2'


def build_report(data: dict, analysis_result: dict, meta: dict | None=None) -> dict:
    """Agent 입력용 자기완결 보고서. 계산 결과는 그대로 두고 출처·품질 정보만 덧붙인다."""
    meta=meta or {}
    return {
        'schema_version':REPORT_SCHEMA_VERSION,
        'generated_at':datetime.now().isoformat(timespec='seconds'),
        'company':{'corp_code':data['corp_code'],'corp_name':data['corp_name'],
                   'stock_code':meta.get('stock_code'),'industry_code':data.get('industry_code')},
        'basis':{'fs_div':data['fs_div'],'report_code':data.get('report_code'),
                 'currency':data.get('currency'),'settlement_month':meta.get('settlement_month')},
        'sources':{k:{'status':v.get('status'),'rcept_no':v.get('rcept_no')}
                   for k,v in (meta.get('year_status') or {}).items()},
        'data_quality':{'consistency_checks':meta.get('consistency_checks',[]),
                        'collected_at':meta.get('collected_at')},
        'financials':data.get('financials'),
        **analysis_result,
    }


def save_analysis_result(data: dict, analysis_result: dict, *, data_dir, meta: dict | None=None) -> Path:
    """LLM Agent/후속 단계가 동일한 정량 결과를 JSON으로 재사용하도록 저장."""
    path=Path(data_dir)/'analysis'/data['corp_code']/data['fs_div']/'quantitative_report.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    payload=build_report(data,analysis_result,meta)
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)
    return path
