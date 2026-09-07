"""Workspace management API for EvoAgentX Studio.

Serves the per-graph workspace (studio/data/workspace/<graph_id>/): run
artifacts (runs/<run_id>/input.json + output.json), tool-produced files
(files/), and user-managed files (upload / create / delete).
"""

import graphs as graph_store
import workspace
from fastapi import APIRouter, Body, HTTPException, Query, UploadFile
from fastapi.responses import Response

router = APIRouter(prefix="/api")


def _check_graph(graph_id: str) -> None:
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")


@router.get("/graphs/{graph_id}/workspace")
def workspace_tree(graph_id: str):
    _check_graph(graph_id)
    return {"graph_id": graph_id, "files": workspace.tree(graph_id)}


@router.get("/graphs/{graph_id}/workspace/file")
def workspace_file(graph_id: str, path: str = Query(...)):
    _check_graph(graph_id)
    try:
        return workspace.read_file(graph_id, path)
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=404 if e.not_found else 400, detail=str(e))


@router.put("/graphs/{graph_id}/workspace/file")
def workspace_write_text(graph_id: str, body: dict = Body(...)):
    """Create/overwrite a text file: {"path": "notes/a.txt", "content": "..."}"""
    _check_graph(graph_id)
    path = body.get("path") or ""
    content = body.get("content") or ""
    try:
        return workspace.write_file(graph_id, path, content.encode("utf-8"))
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/graphs/{graph_id}/workspace/upload")
async def workspace_upload(graph_id: str, file: UploadFile, path: str = Query(default="")):
    """Upload any file; saved under files/ unless `path` overrides."""
    _check_graph(graph_id)
    rel = path or f"files/{file.filename}"
    try:
        return workspace.write_file(graph_id, rel, await file.read())
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/graphs/{graph_id}/workspace/mkdir")
def workspace_mkdir(graph_id: str, body: dict = Body(...)):
    """Create a directory: {"path": "files/batch-1"}"""
    _check_graph(graph_id)
    try:
        return workspace.make_dir(graph_id, body.get("path") or "")
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/graphs/{graph_id}/workspace/download")
def workspace_download(graph_id: str, path: str = Query(default="")):
    """Download one workspace file, or a folder (and the workspace) as a zip."""
    _check_graph(graph_id)
    try:
        name, media, blob = workspace.download(graph_id, path)
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=404 if e.not_found else 400, detail=str(e))
    return Response(
        content=blob, media_type=media,
        headers={"content-disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/graphs/{graph_id}/workspace/file")
def workspace_delete(graph_id: str, path: str = Query(...),
                     recursive: bool = Query(default=False)):
    _check_graph(graph_id)
    try:
        return {"ok": True, **workspace.delete_file(graph_id, path, recursive)}
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=404 if e.not_found else 400, detail=str(e))
