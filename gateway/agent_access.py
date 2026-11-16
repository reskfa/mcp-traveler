"""Download the same Agent skill shipped with the project."""
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()


@router.get('/api/agent/skill')
def agent_skill(download: bool = True):
    candidates = [
        Path(__file__).resolve().parents[1] / 'skills/ashare-gateway/SKILL.md',
        Path(sys.prefix) / 'share/mcp-traveler/skills/ashare-gateway/SKILL.md',
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise HTTPException(404, 'Skill 文件未安装，请从项目 skills/ashare-gateway 获取。')
    return FileResponse(path, media_type='text/plain; charset=utf-8',
                        filename='SKILL.md' if download else None)
