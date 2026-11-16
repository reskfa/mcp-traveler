import asyncio
from pathlib import Path

import httpx

from gateway.app import app


def test_skill_download_matches_repository_and_can_be_read_inline():
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://localhost') as client:
            response = await client.get('/api/agent/skill')
            assert response.status_code == 200
            assert 'attachment' in response.headers['content-disposition']
            expected = Path(__file__).parents[1] / 'skills/ashare-gateway/SKILL.md'
            assert response.text == expected.read_text()
            inline = await client.get('/api/agent/skill?download=0')
            assert inline.text == response.text
            assert 'content-disposition' not in inline.headers
    asyncio.run(check())
