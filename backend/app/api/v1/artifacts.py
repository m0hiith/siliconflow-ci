import logging
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Artifact
from app.services.artifacts import storage_path

router = APIRouter(tags=["artifacts"])
log = logging.getLogger(__name__)


@router.get(
    "/artifacts/{artifact_id}/download",
    response_class=FileResponse,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "headers": {
                "X-Checksum-SHA256": {
                    "description": "Server-computed SHA-256 of the content, lowercase hex",
                    "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                }
            },
        },
        404: {"description": "Artifact not found, or its content is not stored"},
    },
)
async def download_artifact(
    artifact_id: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> FileResponse:
    """Stream an artifact's content. X-Checksum-SHA256 carries the hash the
    server computed when it stored the file."""
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.storage_key is None:
        raise HTTPException(status_code=404, detail="artifact content is not stored")
    path = storage_path(request.app.state.settings.artifact_root, artifact.storage_key)
    if not path.is_file():
        log.error("artifact content missing from the store", extra={"artifact_id": artifact.id})
        raise HTTPException(status_code=404, detail="artifact content is not stored")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=PurePosixPath(artifact.file_name).name,
        headers={"X-Checksum-SHA256": artifact.sha256},
    )
