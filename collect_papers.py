"""Run in the existing Jobtory project folder: python collect_papers.py"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='검색어를 하나씩 조회해 국회도서관 논문을 기존 Supabase papers에 누적합니다.')
    parser.add_argument('--plan', action='store_true', help='키·네트워크 없이 검색어와 수집 범위만 확인')
    parser.add_argument('--keywords', type=Path, default=ROOT / 'collect_keywords.json')
    parser.add_argument('--query', action='append', help='이 검색어만 수집. 여러 번 지정 가능')
    parser.add_argument('--pages', type=int, default=3, help='검색어별 수집할 마지막 페이지. 기본 3')
    parser.add_argument('--page-size', type=int, default=10, help='페이지당 최대 검색 건수. 1~10')
    parser.add_argument('--type', choices=('ARTICLE', 'THESIS'), default='ARTICLE', dest='div_flag')
    parser.add_argument('--fast', action='store_true', help='남은 호출 한도 안에서 1초 간격으로 수집한 뒤 배치 종료')
    parser.add_argument('--interval', type=float, default=None, help='요청 간격. 기본 20초, --fast는 기본 1초')
    parser.add_argument('--request-budget', type=int, default=None, help='이번 실행의 검색+상세 요청 상한. 기본 2000, --fast는 최대 180')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'data' / 'paper_collection')
    parser.add_argument('--reset-progress', action='store_true', help='진행 위치만 초기화. DB 논문은 유지')
    args = parser.parse_args(argv)
    args.interval = (1.0 if args.fast else 20.0) if args.interval is None else args.interval
    args.request_budget = (180 if args.fast else 2000) if args.request_budget is None else args.request_budget
    if not 1 <= args.pages <= 1000 or not 1 <= args.page_size <= 10:
        parser.error('--pages는 1~1000, --page-size는 1~10이어야 합니다.')
    minimum_interval = 1 if args.fast else 20
    if not minimum_interval <= args.interval <= 60 or args.request_budget < 1:
        parser.error(f'--interval은 {minimum_interval}~60초, --request-budget은 1 이상이어야 합니다.')
    if args.fast and args.request_budget > 180:
        parser.error('빠른 배치의 --request-budget은 최대 180회입니다. 이 프로젝트의 남은 시간당 한도도 적용됩니다.')
    try:
        from dotenv import load_dotenv
        from nal_collection import (NalSource, PaperError, Progress, RunLock,
                                    SupabaseStore, collect, read_plan, safe_failure)
        from paper_repository import PaperRepository
        from nal_rate_limit import NalQuota
    except ImportError:
        print('수집 파일을 기존 프로젝트의 app.py·paper_service.py·paper_repository.py 옆에 두세요.')
        print('패키지가 없으면 python -m pip install supabase python-dotenv 를 실행하세요.')
        return 1
    try:
        jobs = read_plan(args.keywords)
        if args.query:
            known = {job['query']: job for job in jobs}
            queries = list(dict.fromkeys(q.strip() for q in args.query))
            if any(not 1 <= len(q) <= 100 for q in queries):
                raise PaperError('검색어는 1~100자여야 합니다.')
            jobs = [known.get(query, {'query': query, 'companies': []}) for query in queries]
        if args.plan:
            for number, job in enumerate(jobs, 1):
                print(f'{number:02d}. {job["query"]} · {", ".join(job["companies"]) or "추가 검색어"}')
            print(f'검색어 {len(jobs)}개 × {args.pages}페이지 × {args.page_size}건 = '
                  f'검색 후보 최대 {len(jobs) * args.pages * args.page_size}건')
            print('실제 저장 수는 검색 결과·중복에 따라 줄어듭니다. 이 명령은 API와 DB를 호출하지 않습니다.')
            print(f'요청 간격 {args.interval:g}초 · 이번 실행 요청 예산 {args.request_budget}회 (시간당 한도와 별도)')
            if args.fast:
                print('빠른 배치: 현재 남은 호출 한도와 위 예산 중 작은 범위까지만 수집한 뒤 종료합니다. 요청 횟수와 논문 편수는 다릅니다.')
            return 0
        load_dotenv(ROOT / '.env')
        missing = [name for name in ('SUPABASE_URL', 'SUPABASE_SECRET_KEY', 'NAL_API_KEY')
                   if not os.getenv(name, '').strip()]
        if missing:
            raise PaperError('기존 .env에 다음 항목을 설정하세요: ' + ', '.join(missing))
        url = os.environ['SUPABASE_URL'].strip().rstrip('/')
        repository = PaperRepository(url, os.environ['SUPABASE_SECRET_KEY'])
        quota = NalQuota(os.environ['NAL_API_KEY'], args.state_dir, max_requests=None)
        source = NalSource(os.environ['NAL_API_KEY'], interval=args.interval,
                           request_budget=args.request_budget, post=quota.post, fast_mode=args.fast)
        database_scope = hashlib.sha256(url.encode()).hexdigest()
        with RunLock(args.state_dir):
            progress = Progress(args.state_dir, database_scope, reset=args.reset_progress)
            if args.fast:
                progress.check_cooldown()
                status = quota.status()
                if status.wait_seconds:
                    raise PaperError(status.message)
                source.budget = min(args.request_budget, status.remaining)
                quota.max_requests = source.budget
                print(f'빠른 배치 v4.3.3 · 요청 간격 {args.interval:g}초 · 최근 1시간 남은 요청 {status.remaining}회 · 이번 배치 최대 {source.budget}회')
            result = collect(jobs, source, SupabaseStore(repository), progress,
                pages=args.pages, page_size=args.page_size, div_flag=args.div_flag)
        if result.status == 'interrupted':
            return 130
        if result.status == 'completed':
            print('지정한 범위의 수집이 끝났습니다. 사이트의 DB 논문 새로고침을 누르세요.')
        elif args.fast and result.status == 'paused_request_budget':
            print('빠른 배치가 끝났습니다. 진행 위치를 저장했으며, 나중에 같은 --fast 명령으로 이어갈 수 있습니다.')
            print(quota.status().message)
        return 0
    except PaperError as exc:
        message = str(exc)
        for name in ('NAL_API_KEY', 'SUPABASE_SECRET_KEY', 'OPENAI_API_KEY'):
            if os.getenv(name):
                message = message.replace(os.environ[name], '[키 생략]')
        print('수집 중단: ' + message)
        print('원인을 해결한 뒤 같은 명령으로 실행하면 마지막 미완료 페이지부터 이어갑니다.')
        return 1
    except KeyboardInterrupt:
        print('수집을 멈췄습니다. 같은 명령으로 다시 실행하세요.')
        return 130
    except Exception as exc:
        print('수집 중단: ' + safe_failure(exc))
        print('진행 기록과 DB 논문은 삭제하지 않았습니다. 원문 오류나 인증키를 제외한 위 진단 줄을 알려 주세요.')
        print('data/paper_collection/last_run.json이 있으면 이번 실행의 저장 건수를 확인할 수 있습니다.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
