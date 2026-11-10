"""Server-side access to the existing public.papers Supabase table."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from paper_service import Paper, PaperError, safe_url


class PaperDatabaseError(PaperError):
    pass


@dataclass(frozen=True)
class PaperBatch:
    papers: list[Paper]
    truncated: bool = False


def paper_from_row(row: dict) -> Paper:
    """Keep source identity and original text; missing abstracts stay empty."""
    if not isinstance(row, dict):
        raise PaperDatabaseError('papers 테이블의 논문 형식을 확인해 주세요.')
    text_fields = ('paper_id', 'title', 'journal', 'publication_year', 'abstract', 'doi',
                   'url', 'open_access_flag', 'query', 'retrieved_at', 'provider',
                   'source_scope', 'div_flag', 'doc_url')
    values = {}
    for name in text_fields:
        value = row.get(name)
        if name == 'publication_year' and isinstance(value, int):
            value = str(value)
        if value is None:
            value = '국회도서관 LOSI' if name == 'provider' else 'abstract' if name == 'source_scope' else ''
        if not isinstance(value, str):
            raise PaperDatabaseError(f'papers 테이블의 {name} 열 형식을 확인해 주세요.')
        values[name] = value
    if not values['paper_id'].strip() or not values['title'].strip():
        raise PaperDatabaseError('papers 테이블에 논문 ID 또는 제목이 없는 행이 있습니다.')
    if values['source_scope'] != 'abstract':
        raise PaperDatabaseError('현재 사이트는 source_scope가 abstract인 논문을 사용합니다.')
    arrays = {}
    for name in ('authors', 'keywords'):
        value = row.get(name) or []
        if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
            raise PaperDatabaseError(f'papers 테이블의 {name} 열은 문자열 배열이어야 합니다.')
        arrays[name] = tuple(value)
    values['url'] = safe_url(values['url'])
    values['doc_url'] = safe_url(values['doc_url'])
    return Paper(**values, **arrays)


def papers_revision(papers: list[Paper]) -> str:
    payload = [p.to_dict() for p in sorted(papers, key=lambda p: p.paper_id)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class PaperRepository:
    """A private server credential is never returned to the UI or error output."""
    def __init__(self, url: str, secret_key: str, *, client=None):
        self.url = url.strip().rstrip('/')
        self._key = secret_key.strip()
        try:
            parsed = urlsplit(self.url)
            valid_url = (parsed.scheme == 'https' and bool(parsed.hostname) and
                         not parsed.username and not parsed.password and
                         parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
        except ValueError:
            valid_url = False
        if not valid_url or not self._key:
            raise PaperDatabaseError('SUPABASE_URL에 프로젝트 HTTPS 주소와 SUPABASE_SECRET_KEY를 함께 설정해 주세요.')
        self._client = client

    @property
    def connection_id(self) -> str:
        return hashlib.sha256((self.url + '|' + self._key).encode()).hexdigest()

    def _db(self):
        if self._client is None:
            try:
                from supabase import create_client
                from supabase.client import ClientOptions
            except ImportError:
                raise PaperDatabaseError('Supabase 패키지가 필요합니다. 터미널에서 python -m pip install supabase 를 실행해 주세요.') from None
            try:
                self._client = create_client(self.url, self._key,
                                             options=ClientOptions(postgrest_client_timeout=10))
            except Exception:
                raise PaperDatabaseError('Supabase 연결 설정을 확인해 주세요. 프로젝트 URL과 서버용 Secret key가 필요합니다.') from None
        return self._client

    def load(self, *, limit: int = 2000, page_size: int = 200) -> PaperBatch:
        if limit < 1 or not 1 <= page_size <= 1000:
            raise ValueError('Invalid page size or limit')
        rows = []
        try:
            while len(rows) <= limit:
                count = min(page_size, limit + 1 - len(rows))
                if count <= 0:
                    break
                batch = (self._db().table('papers').select('*')
                         .order('paper_id').range(len(rows), len(rows) + count - 1).execute()).data
                if not isinstance(batch, list):
                    raise PaperDatabaseError('Supabase에서 논문 목록을 읽지 못했습니다.')
                rows.extend(batch)
                if len(batch) < count:
                    break
        except PaperDatabaseError:
            raise
        except Exception:
            raise PaperDatabaseError('Supabase 논문 조회에 실패했습니다. 프로젝트 URL·Secret key·papers 테이블 권한과 연결 상태를 확인해 주세요.') from None
        return PaperBatch([paper_from_row(row) for row in rows[:limit]], len(rows) > limit)

    def save(self, papers: list[Paper]) -> int:
        if not papers:
            return 0
        rows = {paper.paper_id: paper.to_dict() for paper in papers}
        try:
            self._db().table('papers').upsert(list(rows.values()), on_conflict='paper_id').execute()
        except PaperDatabaseError:
            raise
        except Exception:
            raise PaperDatabaseError('논문은 조회했지만 Supabase에 저장하지 못했습니다. papers 테이블의 저장 권한을 확인해 주세요.') from None
        return len(rows)

    def search(self, queries: list[str], *, div_flag='ARTICLE', limit=120) -> PaperBatch:
        """Filter in Postgres, including rows beyond the manual list's 2,000 limit."""
        if div_flag not in ('ARTICLE', 'THESIS') or not 1 <= limit <= 500:
            raise ValueError('Invalid search settings')
        terms = []
        for query in queries[:2]:
            cleaned = ' '.join(re.findall(r'[가-힣A-Za-z0-9]+', query))
            for term in [cleaned, *cleaned.split()]:
                if len(term) >= 2 and term not in terms:
                    terms.append(term)
        terms = terms[:10]
        if not terms:
            return PaperBatch([])
        filters = ','.join([f'{column}.ilike.%{term.replace(" ", "%")}%'
                           for term in terms for column in ('title', 'abstract')]
                          + [f'keywords.cs.{{"{term}"}}' for term in terms])
        try:
            rows = (self._db().table('papers').select('*').eq('div_flag', div_flag)
                    .or_(filters).order('publication_year', desc=True).order('paper_id')
                    .range(0, limit).execute()).data
            if not isinstance(rows, list):
                raise ValueError
            return PaperBatch([paper_from_row(row) for row in rows[:limit]], len(rows) > limit)
        except PaperDatabaseError:
            raise
        except Exception:
            raise PaperDatabaseError('Supabase에서 질문 관련 논문을 검색하지 못했습니다. 확보된 DB 목록과 API 검색으로 보완합니다.') from None

    def fill_missing_metadata(self, papers: list[Paper]) -> int:
        """Fill blank fields only, guarded by the exact old values to avoid overwrites."""
        from paper_quality import canonical_doi
        fields = ('abstract', 'url', 'doc_url', 'doi', 'publication_year')
        updated = 0
        try:
            for paper in papers:
                rows = (self._db().table('papers').select('paper_id,' + ','.join(fields))
                        .eq('paper_id', paper.paper_id).range(0, 0).execute()).data
                if not isinstance(rows, list) or len(rows) != 1:
                    continue
                old = rows[0]
                if not isinstance(old, dict) or old.get('paper_id') != paper.paper_id:
                    continue
                fresh = {name: getattr(paper, name) for name in fields}
                fresh['doi'] = canonical_doi(paper.doi)
                changes = {name: value for name, value in fresh.items()
                           if not str(old.get(name) or '').strip() and value.strip()}
                if not changes:
                    continue
                query = self._db().table('papers').update(changes).eq('paper_id', paper.paper_id)
                for name in changes:
                    query = query.is_(name, 'null') if old.get(name) is None else query.eq(name, old[name])
                result = query.execute().data
                if isinstance(result, list):
                    updated += len(result)
            return updated
        except Exception:
            raise PaperDatabaseError('API에서 확인한 기존 논문의 빈 항목을 DB에 보완하지 못했습니다. 이번 답변에는 조회한 근거를 사용합니다.') from None

    def save_new(self, papers: list[Paper]) -> int:
        """Keep existing rows and avoid re-inserting another record with a known DOI."""
        from paper_quality import canonical_doi, deduplicate_papers
        if not papers:
            return 0
        known_ids, known_dois, offset = set(), set(), 0
        try:
            while True:
                rows = (self._db().table('papers').select('paper_id,doi').order('paper_id')
                        .range(offset, offset + 499).execute()).data
                if not isinstance(rows, list):
                    raise ValueError
                for row in rows:
                    known_ids.add(row['paper_id'])
                    doi = canonical_doi(row.get('doi') or '')
                    if doi:
                        known_dois.add(doi)
                offset += len(rows)
                if len(rows) < 500:
                    break
            selected = []
            for paper in deduplicate_papers(papers):
                doi = canonical_doi(paper.doi)
                if paper.paper_id in known_ids or (doi and doi in known_dois):
                    continue
                selected.append(paper)
                known_ids.add(paper.paper_id)
                if doi:
                    known_dois.add(doi)
            return self.save(selected)
        except PaperDatabaseError:
            raise
        except Exception:
            raise PaperDatabaseError('API 논문을 DB에 누적하지 못했습니다. 이번 답변에는 조회한 근거를 사용합니다.') from None
