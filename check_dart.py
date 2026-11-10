"""실행: python check_dart.py --company 롯데이노베이트 --year 2025"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dart_service import DartClient, DartError, DartNoData, cached_companies, save_employees

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OpenDART 연결을 확인하고 회사별 직원 현황을 저장합니다.")
    parser.add_argument("--company", default="롯데이노베이트", help="DART에 등록된 회사명")
    parser.add_argument("--year", type=int, default=2025, help="보고서 제출연도가 아닌 사업연도")
    parser.add_argument("--corp-code", help="동명 법인이 있을 때 직접 지정하는 8자리 고유번호")
    parser.add_argument("--refresh-companies", action="store_true", help="회사 고유번호 목록을 새로 내려받습니다.")
    args = parser.parse_args(argv)
    try:
        try:
            from dotenv import load_dotenv
        except ImportError:
            raise DartError("python-dotenv가 필요합니다. python -m pip install python-dotenv 를 실행하세요.") from None
        load_dotenv(ROOT / ".env", override=False)
        client = DartClient(os.getenv("DART_API_KEY", ""))
        directory = ROOT / "dart_data"
        if args.corp_code:
            corp_code = args.corp_code
            print("[1/3] 지정한 회사 고유번호로 조회합니다. 응답의 법인명도 대조합니다.", flush=True)
        else:
            print("[1/3] 회사 고유번호를 확인하고 있습니다. 처음 실행할 때는 기업 목록을 내려받습니다.", flush=True)
            companies = cached_companies(client, directory / "corp_codes.json", args.refresh_companies)
            company = client.resolve_company(args.company, companies)
            corp_code = company["corp_code"]
        print(f"[2/3] 직원 현황 조회 중 · 사업연도 {args.year} · 고유번호 {corp_code}", flush=True)
        snapshot = client.get_employees(corp_code, args.year, expected_name=args.company)
        path = save_employees(snapshot, directory)
        print(f"[3/3] 조회·저장 성공: {snapshot.corp_name} · {snapshot.fiscal_year} 사업연도 · {len(snapshot.rows)}행")
        print("\n사업부문 | 성별 | 직원 합계 | 평균근속 | 1인평균급여(API 원문값)")
        for row in snapshot.rows:
            values = [row.get(k) or "미제공" for k in ("fo_bbm", "sexdstn", "sm", "avrg_cnwk_sdytrn", "jan_salary_am")]
            print(" | ".join(str(v).replace("\n", " ").replace("\r", " ") for v in values))
        print("\n급여 단위·집계 대상·산정 기준은 아래 원문 공시와 함께 확인하세요. 평균값을 임의로 합산하지 않았습니다.")
        print("원문 공시:", snapshot.source_url)
        print("저장 파일:", path)
        print("\n이 단계는 DART 조회·저장 확인입니다. 기존 app.py의 LLM 답변에 자동 연결되는 수정은 포함하지 않습니다.")
        return 0
    except DartNoData as exc:
        print("[자료 없음] " + str(exc), file=sys.stderr)
        print("API 전체 장애와는 다릅니다. 해당 사업연도와 원문 공시의 직원 현황을 확인하세요.", file=sys.stderr)
        return 3
    except DartError as exc:
        print("[연결 확인 실패] " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n조회가 중단되었습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
