"""Local rolling quota shared by the site and this project's collector."""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from paper_service import PaperError, _post_losi

_THREAD_LOCK = threading.RLock()


class NalQuotaError(PaperError):
    pass


@dataclass(frozen=True)
class QuotaStatus:
    remaining: int
    wait_seconds: int

    @property
    def message(self):
        if self.wait_seconds:
            return f'국회도서관 API 대기 중 · 약 {math.ceil(self.wait_seconds / 60)}분 후 재시도 · DB 자료 사용'
        return f'국회도서관 API 보충 가능 · 이 프로젝트 최근 1시간 요청 여유 {self.remaining}회'


def _traffic_error(message):
    message = str(message).casefold()
    return (any(term in message for term in ('트래픽', '트랙픽', 'traffic'))
            and any(term in message for term in ('초과', 'exceed')))


class NalQuota:
    def __init__(self, api_key, directory, *, hourly_limit=180, max_requests=8,
                 clock=time.time, post=None):
        if not api_key.strip():
            raise NalQuotaError('국회도서관 API 키가 없습니다. DB 자료를 사용합니다.')
        if not 1 <= hourly_limit <= 180 or (max_requests is not None and max_requests < 1):
            raise ValueError('Invalid API request budget')
        self.key = api_key.strip()
        self.directory = Path(directory)
        identity = hashlib.sha256(self.key.encode()).hexdigest()[:24]
        self.path = self.directory / 'api_usage' / (identity + '.json')
        self.lock_path = self.path.with_suffix('.lock')
        self.hourly_limit, self.max_requests = hourly_limit, max_requests
        self.clock, self._post = clock, post or _post_losi
        self.calls = 0

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _THREAD_LOCK, self.lock_path.open('a+b') as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise NalQuotaError('다른 요청이 API 사용량을 기록하고 있습니다. DB 자료를 사용합니다.') from None
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'calls': [], 'blocked_until': 0}
            stamps = data['calls']
            blocked = data['blocked_until']
            if (not isinstance(stamps, list) or any(not isinstance(t, (int, float)) or not math.isfinite(t) for t in stamps)
                    or not isinstance(blocked, (int, float)) or not math.isfinite(blocked)):
                raise ValueError
            # The bulk collector also persists provider cooldowns in this file.
            progress_path = self.directory / 'progress.json'
            if progress_path.exists():
                progress = json.loads(progress_path.read_text(encoding='utf-8'))
                cooldown = progress.get('cooldown_until', 0)
                if not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown):
                    raise ValueError
                blocked = max(blocked, cooldown)
            now = self.clock()
            return {'calls': [t for t in stamps if t > now - 3600], 'blocked_until': blocked}
        except (OSError, ValueError, KeyError, TypeError):
            raise NalQuotaError('API 사용량 기록을 읽지 못해 추가 호출을 중지했습니다. DB 자료를 사용합니다.') from None

    def _write(self, data):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(self.path)

    def _status(self, data):
        now = self.clock()
        deadline = data['blocked_until']
        if len(data['calls']) >= self.hourly_limit:
            deadline = max(deadline, min(data['calls']) + 3600)
        return QuotaStatus(max(0, self.hourly_limit - len(data['calls'])), max(0, math.ceil(deadline - now)))

    def status(self):
        return self._status(self._read())

    def post(self, operation, params):
        if operation not in ('searchTotal', 'searchView') or params.get('authKey') != self.key:
            raise NalQuotaError('국회도서관 요청의 주소 또는 키 설정이 다릅니다.')
        if self.max_requests is not None and self.calls >= self.max_requests:
            raise NalQuotaError('이번 질문의 API 추가 검색 예산을 사용했습니다. 확보한 자료로 답변합니다.')
        try:
            with self._locked():
                data = self._read()
                status = self._status(data)
                if status.wait_seconds:
                    raise NalQuotaError(status.message)
                data['calls'].append(self.clock())
                self._write(data)
        except OSError:
            raise NalQuotaError('API 사용량을 기록하지 못해 추가 호출을 중지했습니다. DB 자료를 사용합니다.') from None
        self.calls += 1
        try:
            return self._post(operation, params)
        except PaperError as exc:
            if _traffic_error(exc):
                try:
                    with self._locked():
                        data = self._read()
                        data['blocked_until'] = max(data['blocked_until'], self.clock() + 3600)
                        self._write(data)
                except OSError:
                    raise NalQuotaError('국회도서관 요청 한도에 도달했습니다. 대기 기록을 저장하지 못했으니 최소 1시간 뒤 다시 시도하세요. DB 자료를 사용합니다.') from None
                raise NalQuotaError('국회도서관 요청 한도에 도달해 최소 1시간 API 보충을 중지합니다. 확보한 DB 자료로 답변합니다.') from None
            raise
