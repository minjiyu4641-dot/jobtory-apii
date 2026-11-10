"""DB first, bounded API supplementation, and verified source preservation."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone

from paper_service import PaperError, _plain, _result_nodes, _search_rows, parse_nal_detail
from paper_quality import _same_paper, exclusion_reason, publication_year, research_scope
from paper_repository import papers_revision
from research_service import (ResearchResult, _bounded_plan, create_research_plan,
                              find_stored_research, select_papers)


def api_candidates(query, nal_key, div_flag, *, quota, excluded_ids=(), detail_limit=3,
                   repair_ids=(), question='', current_year=None, max_pages=6):
    """Yield completed details so a later API error cannot discard earlier papers."""
    ids = set(excluded_ids)
    repair_ids = set(repair_ids)
    found, attempted, repaired, previous = 0, 0, 0, None
    checked = set()
    for page in range(1, max_pages + 1):
        payload = quota.post('searchTotal', {'authKey': nal_key, 'searchTerm': query,
            'searchRange': div_flag, 'pageNo': page, 'printRowCnt': 10})
        nodes = _result_nodes(payload, '통합검색', secrets=(nal_key,))
        if any(n.get('searchList') == [] and str(n.get('totalCount', '')).isdigit()
               and (page - 1) * 10 >= int(n['totalCount']) for n in nodes):
            return
        rows = _search_rows(payload)
        if not rows:
            return
        fingerprint = tuple(sorted(_plain(row.get('lodID')) for row in rows))
        if fingerprint == previous:
            raise PaperError('API가 같은 페이지를 반복 반환해 추가 검색을 중지했습니다.')
        previous = fingerprint
        # Prefer new papers; a known incomplete record may be rechecked once per query.
        for summary in sorted(rows, key=lambda row: f'LOSI:{div_flag}:{_plain(row.get("lodID"))}' in ids):
            lod_id = _plain(summary.get('lodID'))
            if not lod_id or _plain(summary.get('divFlag')) not in ('', div_flag):
                raise PaperError('국회도서관 논문 ID·자료유형을 확인하지 못해 추가 검색을 중지했습니다.')
            identity = f'LOSI:{div_flag}:{lod_id}'
            if identity in checked:
                continue
            if identity in ids:
                if identity not in repair_ids or repaired >= 1:
                    continue
                repaired += 1
            detail = quota.post('searchView', {'authKey': nal_key, 'lodID': lod_id, 'divFlag': div_flag})
            paper = parse_nal_detail(detail, summary, query, div_flag)
            ids.add(identity)
            checked.add(identity)
            attempted += 1
            if not exclusion_reason(paper, question, current_year):
                found += 1
            yield paper
            if found >= detail_limit or attempted >= 6:
                return
        if len(rows) < 10:
            return


def find_hybrid_research(*, papers, question, corp_name, fiscal_year, evidence, api_key, nal_key,
                         model, repository=None, quota=None, div_flag='ARTICLE', api_enabled=True,
                         cache=None, planner=None, reviewer=None, search=None, save=None,
                         progress=None, current_year=None, now=None):
    year = current_year or datetime.now(timezone.utc).year
    clock = time.monotonic() if now is None else now
    seed = ['db-api-v2', papers_revision(papers), question, corp_name, fiscal_year, year, model,
            div_flag, api_enabled, hashlib.sha256((api_key + '|' + nal_key).encode()).hexdigest(),
            getattr(repository, 'connection_id', ''), [(e.source_id, e.text) for e in evidence]]
    cache_key = hashlib.sha256(json.dumps(seed, ensure_ascii=False).encode()).hexdigest()
    if cache is not None and cache_key in cache:
        timestamp, cached = cache[cache_key]
        if 0 <= clock - timestamp < 900:
            return replace(cached, from_cache=True)
    plan = _bounded_plan((planner or create_research_plan)(question=question, corp_name=corp_name,
        fiscal_year=fiscal_year, evidence=evidence, api_key=api_key, model=model))
    if not plan.search_needed:
        return ResearchResult(plan, retrieval_mode='DB 우선 + API 보충', api_status='검색 불필요', api_note=plan.reason)
    database_papers = papers
    database_notes = []
    if repository is not None:
        if progress:
            progress('Supabase에서 질문 관련 논문을 먼저 검색하고 있어요…')
        try:
            batch = repository.search(plan.queries, div_flag=div_flag)
            database_papers = batch.papers
            if batch.truncated:
                database_notes.append('DB 검색 결과 중 최근 발행 자료를 우선해 최대 120편의 후보를 검토했습니다.')
        except PaperError as exc:
            database_notes.append(str(exc))
    stored = find_stored_research(papers=database_papers, question=question, corp_name=corp_name,
        fiscal_year=fiscal_year, evidence=evidence, api_key=api_key, model=model, div_flag=div_flag,
        planner=lambda **kwargs: plan, reviewer=reviewer, progress=progress, current_year=year)
    stored.plan = plan  # Empty DB must not turn off an otherwise needed API search.
    stored.queries = plan.queries
    stored.retrieval_mode = 'DB 우선 + API 보충'
    stored.api_status, stored.api_note = '', ''
    stored.quality_notes = [note for note in stored.quality_notes
        if '국회도서관 API 신규 조회는 실행하지 않았습니다' not in note
        and not note.startswith('이번 질문에 사용할 논문이 없습니다.')]
    if not database_papers:
        stored.warnings = []
    stored.quality_notes.extend(database_notes)
    stored.db_selected_count = len(stored.papers)
    stored.paper_origins = {p.paper_id: 'Supabase DB' for p in stored.papers}
    # A failed relevance model cannot validate additional API papers either.
    stop_review = bool(stored.warnings)
    prefer_fresh = research_scope(question) == 'recent'
    needs_api = (len(stored.papers) < 3 or prefer_fresh) and not stop_review
    fresh = None
    api_papers = []
    if needs_api:
        if not api_enabled:
            stored.api_status, stored.api_note = '꺼짐', 'DB 부족 시 API로 보충 설정이 꺼져 있습니다.'
            stored.quality_notes.append('API 보충을 꺼 두어 확보한 DB 논문만 사용합니다.')
        elif not nal_key or (quota is None and search is None):
            stored.api_status, stored.api_note = '설정 확인 필요', '국회도서관 API 키 또는 API 연결 설정을 확인해 주세요.'
            stored.quality_notes.append('국회도서관 API 키가 없어 확보한 DB 논문만 사용합니다.')
        else:
            stored.api_status = '완료'
            stored.api_note = 'DB에서 적합한 논문이 부족해 API에서 새 후보와 불완전한 기존 자료를 확인했습니다.'
            if progress:
                progress('DB에서 부족한 논문을 국회도서관 API로 보충하고 있어요…')
            excluded_ids = {p.paper_id for p in [*papers, *database_papers]}
            repair_ids = {p.paper_id for p in [*papers, *database_papers]
                          if exclusion_reason(p, question, year) in ('초록 미제공', '출처 링크·DOI 미제공', '발행연도 미확인')}
            for query in plan.queries:
                try:
                    stream = (search or api_candidates)(query, nal_key, div_flag,
                        quota=quota, excluded_ids=excluded_ids, detail_limit=3,
                        repair_ids=repair_ids, question=question, current_year=year)
                    for paper in stream:
                        excluded_ids.add(paper.paper_id)
                        repair_ids.discard(paper.paper_id)
                        if not any(_same_paper(paper, old) for old in [*stored.papers, *api_papers]):
                            api_papers.append(paper)
                except PaperError as exc:
                    stored.api_status = '요청 중단'
                    stored.api_note = str(exc)
                    stored.warnings.append(str(exc))
                    break
            if api_papers:
                fresh = select_papers(papers=api_papers, question=question, corp_name=corp_name,
                    fiscal_year=fiscal_year, evidence=evidence, api_key=api_key, model=model,
                    plan=plan, reviewer=reviewer, current_year=year)
                stored.warnings.extend(fresh.warnings)
                for reason, count in fresh.excluded_reasons.items():
                    stored.excluded_reasons[reason] = stored.excluded_reasons.get(reason, 0) + count
                if fresh.papers and (save is not None or repository is not None):
                    try:
                        stored.api_saved_count = (save or repository.save_new)(fresh.papers)
                    except PaperError as exc:
                        stored.warnings.append(str(exc))
                if fresh.papers and repository is not None and save is None and hasattr(repository, 'fill_missing_metadata'):
                    try:
                        stored.api_updated_count = repository.fill_missing_metadata(fresh.papers)
                    except PaperError as exc:
                        stored.warnings.append(str(exc))
    source_papers = [(p, 'Supabase DB') for p in stored.papers]
    if fresh is not None:
        source_papers.extend((p, '국회도서관 API') for p in fresh.papers)
        stored.connections.update(fresh.connections)
        if prefer_fresh:
            source_papers.sort(key=lambda item: publication_year(item[0]) or 0, reverse=True)
    accepted, origins = [], {}
    for paper, source in source_papers:
        if any(_same_paper(paper, old) for old in accepted):
            continue
        accepted.append(paper)
        origins[paper.paper_id] = source
        if len(accepted) == 3:
            break
    from paper_service import make_paper_evidence
    stored.papers = accepted
    stored.evidence = [make_paper_evidence(paper) for paper in accepted]
    stored.connections = {p.paper_id: stored.connections[p.paper_id] for p in accepted if p.paper_id in stored.connections}
    stored.paper_origins = origins
    stored.db_selected_count = sum(source == 'Supabase DB' for source in origins.values())
    stored.api_selected_count = sum(source == '국회도서관 API' for source in origins.values())
    stored.api_request_count = quota.calls if quota is not None else 0
    stored.search_counts = {'Supabase DB': len(database_papers), '국회도서관 API': len(api_papers)}
    stored.candidate_count = len(database_papers) + len(api_papers)
    stored.quality_notes.append(f'DB에서 연결 {stored.db_selected_count}편 · API에서 연결 {stored.api_selected_count}편 · DB에 추가 저장 {stored.api_saved_count}편')
    if not needs_api:
        stored.api_status = '검토 오류로 중단' if stop_review else 'DB 근거 충분'
        stored.api_note = ('논문 관련성 검토에 실패해 API 후보를 추가로 검증할 수 없습니다.' if stop_review
                           else 'DB에서 적합한 논문 3편을 확보해 API 보충이 필요하지 않습니다.')
        stored.quality_notes.append('논문 관련성 검토에 실패하여 API 추가 검색을 실행하지 않았습니다.' if stop_review
                                    else 'DB의 적합한 논문이 충분해 국회도서관 API를 호출하지 않았습니다.')
    if cache is not None and not stored.warnings and stored.evidence:
        if len(cache) >= 20:
            cache.pop(next(iter(cache)))
        cache[cache_key] = (clock, stored)
    return stored
