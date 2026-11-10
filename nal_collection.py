"""Sequential LOSI collection for Jobtory v4.2; no LLM calls or schema changes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from paper_service import PaperError, _plain, _post_losi, _result_nodes, _search_rows, parse_nal_detail


class BudgetReached(Exception):
    pass


class TrafficLimitReached(PaperError):
    pass


def normalize_doi(value):
    doi = str(value or '').strip()
    if doi.lower().startswith(('https://doi.org/', 'http://doi.org/',
                               'https://dx.doi.org/', 'http://dx.doi.org/')):
        doi = unquote(urlsplit(doi).path.lstrip('/'))
    doi = re.sub(r'^doi\s*:\s*', '', doi, flags=re.I).strip().casefold()
    return doi if re.fullmatch(r'10\.\d{4,9}/[^\s<>]+', doi) else ''


def read_plan(path):
    try:
        plan = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        jobs = {}
        for group in plan['groups']:
            companies = group['companies']
            if not isinstance(companies, list) or any(not isinstance(c, str) for c in companies):
                raise ValueError
            for query in group['queries']:
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
                    raise ValueError
                query = query.strip()
                job = jobs.setdefault(query, {'query': query, 'companies': []})
                job['companies'] = sorted(set(job['companies']) | set(companies))
        if not jobs:
            raise ValueError
        return list(jobs.values())
    except (OSError, ValueError, KeyError, TypeError):
        raise PaperError('collect_keywords.json의 groups·companies·queries 형식을 확인하세요.') from None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Windows may briefly deny replacement while the site/editor reads the file.
        # Give each writer its own temp file and close it before the atomic replace.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        for attempt, delay in enumerate((0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 0)):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 6:
                    raise
                time.sleep(delay)
    except OSError as exc:
        code = getattr(exc, 'winerror', None) or exc.errno
        raise PaperError(f'수집 기록 {path.name} 저장에 실패했습니다({type(exc).__name__}, 코드 {code}). '
                         '진행 기록 파일을 읽는 프로그램의 파일 점유·폴더 쓰기 권한을 확인한 뒤 같은 명령으로 다시 실행하세요. '
                         '기존 기록과 이미 저장한 DB 논문은 삭제하지 않았습니다.') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def safe_failure(exc):
    """Expose exception class and our code location, never raw messages/keys."""
    allowed = {'nal_collection.py', 'nal_rate_limit.py', 'paper_service.py', 'paper_repository.py'}
    frames = [frame for frame in traceback.extract_tb(exc.__traceback__)
              if Path(frame.filename).name in allowed]
    location = f'{Path(frames[-1].filename).name}:{frames[-1].lineno}' if frames else '수집 처리'
    code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
    suffix = f' · 오류 코드 {code}' if isinstance(code, int) else ''
    return f'{type(exc).__name__} · 위치 {location}{suffix}'


def append_json(path, value):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


class RunLock:
    """OS lock releases even after a crash; works on Windows and Linux."""
    def __init__(self, directory):
        self.path = Path(directory) / 'collector.lock'
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise PaperError('같은 수집 폴더에서 이미 수집 중입니다. 실행 중인 터미널을 확인하세요.') from None
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()


class Progress:
    def __init__(self, directory, database_scope, reset=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'progress.json'
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                if (self.data['version'] != 1 or not isinstance(self.data['jobs'], dict)
                        or not isinstance(self.data['aliases'], dict)):
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise PaperError('수집 진행 파일을 읽지 못했습니다. 다른 --state-dir로 실행하세요.') from None
            if self.data.get('database_scope') != database_scope:
                raise PaperError('이 수집 기록은 다른 Supabase 프로젝트의 기록입니다. 다른 --state-dir를 사용하세요.')
        else:
            self.data = {'version': 1, 'database_scope': database_scope, 'jobs': {}, 'aliases': {}}
        if reset:
            self.data['jobs'] = {}

    def save(self):
        atomic_json(self.path, self.data)

    def check_cooldown(self, now=None):
        deadline = self.data.get('cooldown_until', 0)
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise PaperError('수집 대기 정보가 올바르지 않습니다. progress.json을 확인하세요.')
        remaining = deadline - (time.time() if now is None else now)
        if remaining > 0:
            raise PaperError(f'이전 트래픽 초과 이후 대기 중입니다. 약 {math.ceil(remaining / 60)}분 후 같은 명령으로 실행하세요.')

    def job(self, query, div_flag, page_size):
        key = hashlib.sha256(json.dumps([query, div_flag, page_size], ensure_ascii=False).encode()).hexdigest()
        entry = self.data['jobs'].setdefault(key, {'query': query, 'type': div_flag,
            'page_size': page_size, 'next_page': 1, 'exhausted': False, 'last_fingerprint': ''})
        if not isinstance(entry, dict):
            raise PaperError(f'검색어 「{query}」의 진행 기록 형식이 올바르지 않습니다. progress.json을 지우지 말고 확인해 주세요.')
        entry.setdefault('exhausted', False)
        entry.setdefault('last_fingerprint', '')
        if (not isinstance(entry.get('next_page'), int) or entry['next_page'] < 1
                or not isinstance(entry.get('exhausted'), bool)):
            raise PaperError(f'검색어 「{query}」의 next_page 또는 exhausted 형식을 확인해 주세요. 기존 진행 기록은 유지했습니다.')
        return entry


class NalSource:
    def __init__(self, api_key, *, interval=20.0, request_budget=2000, post=None, sleep=time.sleep,
                 fast_mode=False):
        if not api_key.strip():
            raise PaperError('NAL_API_KEY가 비어 있습니다.')
        minimum_interval = 1 if fast_mode else 20
        if not minimum_interval <= interval <= 60:
            raise PaperError(f'요청 간격은 {minimum_interval}~60초로 설정하세요.')
        if fast_mode and (post is None or not 1 <= request_budget <= 180):
            raise PaperError('빠른 배치는 호출량 관리와 최대 180회의 실행 예산이 필요합니다.')
        self.key = api_key.strip()
        self.interval = interval
        self.budget = request_budget
        self.calls = 0
        self.fast_mode = fast_mode
        self.post = post or _post_losi
        self.sleep = sleep

    def request(self, operation, params):
        if self.calls >= self.budget:
            raise BudgetReached
        if self.calls:
            self.sleep(self.interval)
        self.calls += 1
        try:
            return self.post(operation, {'authKey': self.key, **params})
        except PaperError as exc:
            message = str(exc).casefold()
            if (any(term in message for term in ('트래픽', '트랙픽', 'traffic'))
                    and any(term in message for term in ('초과', 'exceed'))):
                raise TrafficLimitReached('국회도서관의 시간당 요청 한도에 도달했습니다.') from None
            raise

    def search(self, query, page, page_size, div_flag):
        payload = self.request('searchTotal', {'searchTerm': query,
            'searchRange': div_flag, 'pageNo': page, 'printRowCnt': page_size})
        nodes = _result_nodes(payload, '통합검색', secrets=(self.key,))
        for node in nodes:
            total = str(node.get('totalCount', ''))
            if (node.get('searchList') == [] and total.isdigit()
                    and (page - 1) * page_size >= int(total)):
                return []
        rows = _search_rows(payload)
        if len(rows) > page_size:
            raise PaperError('검색 응답의 페이지 크기가 요청보다 큽니다. 수집을 중단합니다.')
        for row in rows:
            if not _plain(row.get('lodID')) or _plain(row.get('divFlag')) not in ('', div_flag):
                raise PaperError('검색 결과의 논문 ID 또는 자료유형이 다릅니다. 수집을 중단합니다.')
        return rows

    def detail(self, summary, query, div_flag):
        payload = self.request('searchView', {'lodID': _plain(summary['lodID']), 'divFlag': div_flag})
        return parse_nal_detail(payload, summary, query, div_flag)


class SupabaseStore:
    def __init__(self, repository):
        self.repository = repository

    def catalog(self):
        rows, offset, page_size = {}, 0, 500
        try:
            while True:
                batch = (self.repository._db().table('papers').select('paper_id,doi,abstract')
                    .order('paper_id').range(offset, offset + page_size - 1).execute()).data
                if not isinstance(batch, list):
                    raise ValueError
                for row in batch:
                    if not isinstance(row, dict) or not isinstance(row.get('paper_id'), str):
                        raise ValueError
                    rows[row['paper_id']] = row
                offset += len(batch)
                if len(batch) < page_size:
                    return rows
        except Exception:
            raise PaperError('Supabase papers 목록 조회에 실패했습니다. URL·서버용 키·테이블 권한을 확인하세요.') from None

    def save(self, paper):
        self.repository.save([paper])

    def fill_abstract(self, paper_id, abstract):
        try:
            self.repository._db().table('papers').update({'abstract': abstract}).eq('paper_id', paper_id).execute()
        except Exception:
            raise PaperError('DOI 중복 논문의 빈 초록 보완을 저장하지 못했습니다. 같은 명령을 다시 실행하세요.') from None


@dataclass
class Statistics:
    initial_db_papers: int = 0
    pages: int = 0
    details: int = 0
    saved: int = 0
    saved_with_abstract: int = 0
    existing_id: int = 0
    doi_duplicates: int = 0
    abstracts_filled: int = 0
    requests: int = 0
    status: str = 'running'


def collect(jobs, source, store, progress, *, pages=3, page_size=10,
            div_flag='ARTICLE', emit=print, wallclock=time.time):
    progress.check_cooldown(wallclock())
    catalog = store.catalog()
    ids = set(catalog)
    doi_index = {}
    for identity, row in catalog.items():
        doi = normalize_doi(row.get('doi'))
        if doi:
            doi_index.setdefault(doi, identity)
    aliases = progress.data['aliases']
    stats = Statistics(initial_db_papers=len(ids))
    emit('수집 재개 점검 v4.3.3 · 기존 진행 기록과 DB 논문을 유지합니다.')
    emit(f'DB에 이미 저장된 논문 {len(ids)}편 · 검색어 {len(jobs)}개')
    progress.save()
    try:
        for number, job in enumerate(jobs, 1):
            query = job['query']
            cursor = progress.job(query, div_flag, page_size)
            progress.save()
            if cursor['exhausted'] or cursor['next_page'] > pages:
                emit(f'[{number}/{len(jobs)}] {query} · 설정 범위 수집 완료')
                continue
            while cursor['next_page'] <= pages:
                page = cursor['next_page']
                emit(f'[{number}/{len(jobs)}] {query} · {page}/{pages}페이지 조회')
                rows = source.search(query, page, page_size, div_flag)
                fingerprint = hashlib.sha256(json.dumps(sorted(_plain(r['lodID']) for r in rows)).encode()).hexdigest()
                if rows and fingerprint == cursor['last_fingerprint']:
                    raise PaperError('다음 페이지에서도 같은 목록이 반환되어 중단했습니다. API 페이지 동작을 확인하세요.')
                page_before = stats.saved
                for summary in rows:
                    identity = f'LOSI:{div_flag}:{_plain(summary["lodID"])}'
                    append_json(progress.directory / 'matches.jsonl', {'paper_id': identity,
                        'query': query, 'companies': job['companies'], 'page': page})
                    if identity in ids or aliases.get(identity) in ids:
                        stats.existing_id += 1
                        continue
                    paper = source.detail(summary, query, div_flag)
                    stats.details += 1
                    doi = normalize_doi(paper.doi)
                    canonical = doi_index.get(doi) if doi else None
                    # Keep a local source record even if its DOI already exists in the DB.
                    append_json(progress.directory / 'papers_backup.jsonl', {
                        'paper': paper.to_dict(), 'companies': job['companies'],
                        'canonical_paper_id': canonical or paper.paper_id})
                    if canonical and canonical != identity:
                        if paper.abstract.strip() and not (catalog[canonical].get('abstract') or '').strip():
                            store.fill_abstract(canonical, paper.abstract)
                            catalog[canonical]['abstract'] = paper.abstract
                            stats.abstracts_filled += 1
                        aliases[identity] = canonical
                        progress.save()
                        stats.doi_duplicates += 1
                        continue
                    store.save(paper)  # Commit first. An interrupted page is safe to replay.
                    ids.add(identity)
                    catalog[identity] = {'paper_id': identity, 'doi': paper.doi, 'abstract': paper.abstract}
                    if doi:
                        doi_index[doi] = identity
                    stats.saved += 1
                    stats.saved_with_abstract += bool(paper.abstract.strip())
                cursor.update(next_page=page + 1, exhausted=len(rows) < page_size,
                              last_fingerprint=fingerprint)
                progress.save()
                stats.pages += 1
                emit(f'  검색 {len(rows)}건 · 이번 페이지 저장 {stats.saved - page_before}편 · 이번 실행 저장 누계 {stats.saved}편')
                if cursor['exhausted']:
                    break
        stats.status = 'completed'
    except BudgetReached:
        stats.status = 'paused_request_budget'
        if getattr(source, 'fast_mode', False):
            emit('이번 빠른 배치의 요청 범위까지 수집했습니다. 진행 기록을 유지한 채 종료합니다.')
        else:
            emit('이번 실행의 API 요청 예산에 도달했습니다. 같은 명령을 실행하면 이어서 수집합니다.')
    except TrafficLimitReached:
        stats.status = 'paused_provider_limit'
        progress.data['cooldown_until'] = wallclock() + 3600
        progress.save()
        emit('국회도서관 요청 한도 초과로 멈췄습니다. 지금부터 최소 1시간 대기한 뒤 같은 명령으로 이어서 실행하세요.')
        emit('제공처의 정확한 한도 초기화 시각은 확인되지 않았습니다. 계속 제한되면 추가 대기하거나 제공처에 문의하세요.')
    except KeyboardInterrupt:
        stats.status = 'interrupted'
        emit('사용자가 수집을 멈췄습니다. 저장한 논문은 유지되며 같은 명령으로 이어갈 수 있습니다.')
    except PaperError:
        stats.status = 'error'
        raise
    except Exception as exc:
        stats.status = 'error'
        raise PaperError('수집 처리 오류: ' + safe_failure(exc) + '. 원문 오류나 인증키는 표시하지 않았습니다. '
                         '이 진단 줄을 알려 주세요. 진행 기록을 초기화할 필요는 없습니다.') from None
    finally:
        stats.requests = source.calls
        atomic_json(progress.directory / 'last_run.json', asdict(stats))
        emit(f'이번 실행: 저장 처리 {stats.saved}편 · 그중 초록 있음 {stats.saved_with_abstract}편 · '
             f'기존 ID {stats.existing_id}건 · DOI 중복 {stats.doi_duplicates}건 · '
             f'빈 초록 보완 {stats.abstracts_filled}편 · API 요청 {stats.requests}회')
    return stats
