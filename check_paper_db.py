"""Check the local .env and existing papers table without any LLM request."""
import os
from pathlib import Path
from dotenv import load_dotenv
from paper_repository import PaperDatabaseError, PaperRepository


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    try:
        batch = PaperRepository(os.getenv('SUPABASE_URL', ''),
                                os.getenv('SUPABASE_SECRET_KEY', '')).load()
    except PaperDatabaseError as exc:
        print(str(exc))
        return 1
    print(f'Supabase DB 연결 성공 · 불러온 논문 {len(batch.papers)}편')
    for paper in batch.papers[:5]:
        print(f'- {paper.title} ({paper.publication_year or "연도 미제공"})')
    if batch.truncated:
        print('ID 순서로 최대 2,000편을 불러왔습니다.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
