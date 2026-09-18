# routes/personal_routes.py
"""Routes for personal documents management."""
import asyncio
import os
import logging
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from src.request_models import DirectoryRequest
from core.constants import BASE_DIR, PERSONAL_DIR, PERSONAL_UPLOADS_DIR
from src.rag_singleton import get_rag_manager
from src.auth_helpers import require_privilege, require_user
from core.middleware import INTERNAL_TOOL_USER, require_admin
from src.upload_handler import secure_filename
from src.upload_limits import PERSONAL_UPLOAD_MAX_BYTES
from src.rag_sensitivity import SENSITIVITY_PUBLIC, normalize_sensitivity

UPLOADS_DIR = PERSONAL_UPLOADS_DIR

logger = logging.getLogger(__name__)

VAULT_EDITOR_MAX_BYTES = 5 * 1024 * 1024


class VaultFileUpdate(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)
    content: str = Field(..., max_length=VAULT_EDITOR_MAX_BYTES)
    modified: Optional[float] = None


def _personal_upload_dir_for_owner(owner: str | None, *, create: bool = True) -> str:
    """Return the per-owner upload directory used for direct RAG uploads."""
    owner_segment = secure_filename((owner or "local").strip())[:80] or "local"
    upload_dir = os.path.abspath(os.path.join(UPLOADS_DIR, owner_segment))
    base_abs = os.path.abspath(UPLOADS_DIR)
    if os.path.commonpath([upload_dir, base_abs]) != base_abs:
        raise ValueError("Unsafe upload owner path")
    if create:
        os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _unique_personal_upload_path(upload_dir: str, original_name: str | None) -> Tuple[str, str, str]:
    """Build a collision-resistant upload path while preserving a display name."""
    safe_name = secure_filename(os.path.basename(original_name or "upload"))
    if not safe_name or safe_name.startswith("."):
        safe_name = "upload"

    stem, ext = os.path.splitext(safe_name)
    stem = (stem or "upload")[:80]
    filename = f"{stem}-{uuid.uuid4().hex[:10]}{ext.lower()}"
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    upload_abs = os.path.abspath(upload_dir)
    if os.path.commonpath([file_path, upload_abs]) != upload_abs:
        raise ValueError("Unsafe upload filename")
    return file_path, filename, safe_name


def _unique_existing_target(path: str) -> str:
    """Return a non-existing sibling path for rename collision handling."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    while True:
        candidate = f"{stem}-{uuid.uuid4().hex[:10]}{ext}"
        if not os.path.exists(candidate):
            return candidate


def _remove_empty_tree(path: str) -> None:
    """Best-effort removal of empty directories under ``path``."""
    if not os.path.isdir(path):
        return
    for root, dirs, _files in os.walk(path, topdown=False):
        for dirname in dirs:
            candidate = os.path.join(root, dirname)
            try:
                os.rmdir(candidate)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def rename_personal_upload_owner(
    old_owner: str,
    new_owner: str,
    *,
    personal_docs_manager: Any = None,
    rag_manager: Any = None,
) -> Dict[str, Any]:
    """Move direct personal uploads and rewrite RAG owner metadata on user rename."""
    old_dir = _personal_upload_dir_for_owner(old_owner, create=False)
    new_dir = _personal_upload_dir_for_owner(new_owner, create=False)
    path_map: Dict[str, str] = {}
    moved_files = 0

    if os.path.isdir(old_dir) and old_dir != new_dir:
        os.makedirs(new_dir, exist_ok=True)
        for root, _dirs, files in os.walk(old_dir):
            rel_root = os.path.relpath(root, old_dir)
            target_root = new_dir if rel_root == "." else os.path.join(new_dir, rel_root)
            os.makedirs(target_root, exist_ok=True)
            for filename in files:
                source = os.path.abspath(os.path.join(root, filename))
                target = _unique_existing_target(os.path.abspath(os.path.join(target_root, filename)))
                shutil.move(source, target)
                path_map[source] = target
                moved_files += 1
        _remove_empty_tree(old_dir)

    if personal_docs_manager is not None:
        rename_directory = getattr(personal_docs_manager, "rename_directory", None)
        if callable(rename_directory):
            rename_directory(old_dir, new_dir, path_map=path_map)

    rag_result = None
    if rag_manager is not None:
        rename_owner = getattr(rag_manager, "rename_owner", None)
        if callable(rename_owner):
            rag_result = rename_owner(
                old_owner,
                new_owner,
                path_map=path_map,
                path_prefixes=[(old_dir, new_dir)],
            )

    return {
        "old_dir": old_dir,
        "new_dir": new_dir,
        "moved_files": moved_files,
        "path_map": path_map,
        "rag_result": rag_result,
    }


def setup_personal_routes(personal_docs_manager, rag_manager, rag_available):
    """
    Setup personal documents related routes.

    Args:
        personal_docs_manager: PersonalDocsManager instance
        rag_manager: RAG manager instance (may be None)
        rag_available: Boolean indicating if RAG is available

    Returns:
        APIRouter instance with personal docs routes
    """
    router = APIRouter(prefix="/api/personal")

    def _require_human_user(request: Request) -> str:
        """Authenticate a UI caller without admitting agent loopback tokens."""
        owner = require_user(request)
        if owner == INTERNAL_TOOL_USER:
            raise HTTPException(403, "The vault editor is available only to human UI sessions")
        return owner

    # Serializes directory index jobs across requests. Indexing runs in the
    # threadpool (#5558), so concurrent requests would otherwise run in parallel
    # and race PersonalDocsManager's unsynchronized list mutations and file
    # writes; before the threadpool move they serialized on the blocked event
    # loop, so one-at-a-time is behavior parity.
    #
    # An asyncio.Lock acquired in the async handler BEFORE offloading: a waiting
    # request parks on the event loop instead of pinning a threadpool worker (an
    # earlier threading.Lock taken INSIDE the worker meant queued jobs held pool
    # tokens while blocked, starving every other run_in_threadpool caller).
    # add/remove/reload all take this lock, so their mutations never interleave.
    # Per-router (not module-global) so each app binds it to its own event loop.
    # Scope is the single process: multi-worker deployments would need a shared
    # lock (out of scope for #5558).
    _index_job_lock = asyncio.Lock()

    def _rag():
        """Get the current RAG manager, retrying init if needed."""
        return get_rag_manager()

    def _resolve_allowed_personal_dir(directory: str) -> str:
        """Resolve a user-supplied personal-docs path under the allowed root."""
        if not directory:
            raise HTTPException(400, "Directory path is required")

        # realpath (not abspath) so a symlink inside PERSONAL_DIR that points
        # outside it is resolved before the commonpath confinement check below;
        # abspath only normalises `..` and would let such a symlink escape.
        base_abs = os.path.realpath(PERSONAL_DIR)
        candidate = directory if os.path.isabs(directory) else os.path.join(base_abs, directory)
        resolved = os.path.realpath(candidate)
        try:
            in_base = os.path.commonpath([resolved, base_abs]) == base_abs
        except ValueError:
            in_base = False
        if not in_base:
            raise HTTPException(403, "Directory must be inside personal documents")
        return resolved

    def _vault_root() -> str:
        # Use the exact same configured root as sensitivity/readonly policy.
        # App initialization points PersonalDocsManager here as well, but
        # keeping this route coupled to the policy source prevents a stale
        # manager instance from producing incorrect badges.
        from src.rag_sensitivity import vault_root

        root = os.path.realpath(vault_root())
        if not os.path.isdir(root):
            raise HTTPException(404, "Configured vault directory is not mounted or does not exist")
        return root

    def _resolve_vault_file(relative_path: str) -> Tuple[str, str]:
        """Resolve a human-selected Markdown file inside the active vault."""
        raw = str(relative_path or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or os.path.isabs(raw):
            raise HTTPException(400, "A vault-relative file path is required")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise HTTPException(403, "Path must stay inside the vault")
        rel = "/".join(parts)
        if os.path.splitext(rel)[1].lower() not in (".md", ".markdown"):
            raise HTTPException(400, "Only Markdown vault files can be edited")
        root = _vault_root()
        target = os.path.realpath(os.path.join(root, *parts))
        try:
            inside = os.path.commonpath([target, root]) == root
        except ValueError:
            inside = False
        if not inside:
            raise HTTPException(403, "Path must stay inside the vault")
        if not os.path.isfile(target):
            raise HTTPException(404, "Vault file not found")
        return target, rel

    def _vault_file_policy(path: str) -> Dict[str, Any]:
        from src.rag_sensitivity import path_is_readonly, resolve_sensitivity
        from src.vault_markdown import split_frontmatter

        frontmatter = None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                header = handle.read(65537)
            frontmatter, _body = split_frontmatter(header)
        except (OSError, UnicodeError, ValueError):
            frontmatter = None
        return {
            "sensitivity": resolve_sensitivity(path, frontmatter=frontmatter),
            "readonly": path_is_readonly(path),
            # These labels constrain model/agent access, not the authenticated
            # human editor exposed by the routes below.
            "policy_scope": "llm_only",
        }

    def _vault_tree_node(directory: str, rel_dir: str = "") -> Dict[str, Any]:
        children: List[Dict[str, Any]] = []
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name.casefold()),
            )
        except OSError as exc:
            raise HTTPException(500, f"Could not read vault directory: {exc}")

        for entry in entries:
            if entry.name.startswith(".") or entry.name in {"__pycache__", "node_modules"}:
                continue
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir(follow_symlinks=False):
                node = _vault_tree_node(entry.path, rel)
                if node["children"]:
                    children.append(node)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if os.path.splitext(entry.name)[1].lower() not in (".md", ".markdown"):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            children.append({
                "type": "file",
                "name": entry.name,
                "path": rel.replace("\\", "/"),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                **_vault_file_policy(entry.path),
            })
        return {
            "type": "directory",
            "name": os.path.basename(directory.rstrip(os.sep)) or "Vault",
            "path": rel_dir.replace("\\", "/"),
            "children": children,
        }
    
    @router.get("")
    def api_personal_list(owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Enhanced version that includes directories"""
        files = [
            {
                "name": f["name"],
                "size": f["size"],
                "path": f.get("path", ""),
                "sensitivity": f.get("sensitivity", SENSITIVITY_PUBLIC),
            }
            for f in personal_docs_manager.index
        ]
        directories = personal_docs_manager.get_indexed_directories() if hasattr(personal_docs_manager, "get_indexed_directories") else []
        directory_sensitivity = dict(getattr(personal_docs_manager, "directory_sensitivity", {}) or {})
        # Paired list so a client never has to re-derive the label by looking a
        # directory string up in the map: the map is keyed by abspath while a
        # legacy indexed_directories.json may hold unnormalised entries, and a
        # miss there would silently render a private folder as public.
        pair_labels = getattr(personal_docs_manager, "get_indexed_directories_with_sensitivity", None)
        if callable(pair_labels):
            directories_detail = pair_labels(allow_private=True)
        else:
            directories_detail = [
                {
                    "directory": d,
                    "sensitivity": directory_sensitivity.get(
                        os.path.abspath(d), SENSITIVITY_PUBLIC
                    ),
                }
                for d in directories
            ]
        return {
            "files": files,
            "directories": directories,
            "directories_detail": directories_detail,
            "directory_sensitivity": directory_sensitivity,
        }

    @router.get("/vault/tree")
    def api_vault_tree(
        owner: str = Depends(_require_human_user),
    ):
        """Return every Markdown file in the active vault for the human UI."""
        root = _vault_root()
        tree = _vault_tree_node(root)
        return {
            "root_name": tree["name"],
            "tree": tree,
            "policy_scope": "llm_only",
        }

    @router.get("/vault/file")
    def api_vault_file(
        path: str = Query(...),
        owner: str = Depends(_require_human_user),
    ):
        """Open a Markdown file for an authenticated human UI session."""
        target, rel = _resolve_vault_file(path)
        try:
            if os.path.getsize(target) > VAULT_EDITOR_MAX_BYTES:
                raise HTTPException(413, "Vault file is too large for the browser editor")
            with open(target, "r", encoding="utf-8") as handle:
                content = handle.read(VAULT_EDITOR_MAX_BYTES + 1)
        except HTTPException:
            raise
        except (OSError, UnicodeError) as exc:
            raise HTTPException(500, f"Could not read vault file: {exc}")
        if len(content.encode("utf-8")) > VAULT_EDITOR_MAX_BYTES:
            raise HTTPException(413, "Vault file is too large for the browser editor")
        return {
            "path": rel,
            "name": os.path.basename(rel),
            "content": content,
            "modified": os.path.getmtime(target),
            **_vault_file_policy(target),
        }

    @router.put("/vault/file")
    def update_vault_file(
        body: VaultFileUpdate,
        owner: str = Depends(_require_human_user),
    ):
        """Save a vault file as a human, independent of the LLM policy labels."""
        target, rel = _resolve_vault_file(body.path)
        payload = body.content
        if len(payload.encode("utf-8")) > VAULT_EDITOR_MAX_BYTES:
            raise HTTPException(413, "Vault file is too large for the browser editor")
        if body.modified is not None:
            try:
                current_modified = os.path.getmtime(target)
            except OSError as exc:
                raise HTTPException(404, "Vault file is no longer available") from exc
            if abs(current_modified - body.modified) > 0.000001:
                raise HTTPException(
                    409,
                    "This file changed outside Odysseus. Reopen it before saving so those changes are not overwritten.",
                )

        temp_name = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".vault-edit-", suffix=".tmp", dir=os.path.dirname(target))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = None
        except PermissionError as exc:
            raise HTTPException(
                409,
                "The host filesystem mount is read-only; make the vault mount writable for human editing.",
            ) from exc
        except OSError as exc:
            raise HTTPException(500, f"Could not save vault file: {exc}") from exc
        finally:
            if temp_name and os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

        # Update the shared embedding corpus immediately. Failure here does not
        # roll back the human's file edit; the periodic scanner will retry it.
        indexed = False
        rag = _rag()
        if rag:
            try:
                rag.delete_by_source(target)
                owner_for = getattr(rag, "owner_for_directory", None)
                chunk_owner = owner_for(os.path.dirname(target)) if callable(owner_for) else None
                policy = _vault_file_policy(target)
                indexed_count, _failed = rag.index_file(
                    target,
                    owner=chunk_owner,
                    sensitivity=policy["sensitivity"],
                )
                indexed = bool(indexed_count)
            except Exception as exc:
                logger.warning("Immediate re-index failed for human vault edit %s: %s", target, exc)
        try:
            personal_docs_manager.refresh_index()
        except Exception as exc:
            logger.warning("Keyword index refresh failed after human vault edit %s: %s", target, exc)

        return {
            "success": True,
            "path": rel,
            "indexed": indexed,
            "modified": os.path.getmtime(target),
            **_vault_file_policy(target),
        }
    
    @router.post("/reload")
    async def api_personal_reload(owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        # refresh_index() re-extracts text across every tracked directory —
        # blocking work. Take the shared job lock (so it cannot race an add /
        # remove) and run it off the event loop.
        async with _index_job_lock:
            await run_in_threadpool(personal_docs_manager.refresh_index)
        return {"ok": True, "count": len(personal_docs_manager.index)}
    
    @router.post("/add_directory")
    async def add_directory_to_rag(
        request: Request,
        directory_request: DirectoryRequest,
        owner: str = Depends(require_user), _admin: None = Depends(require_admin),
    ):
        """
        Add a directory and all its subdirectories/files to the RAG index.
        
        Args:
            directory_request: Directory request model containing the directory path
            
        Returns:
            JSON response with indexing results
        """
        directory = directory_request.directory
        sensitivity = normalize_sensitivity(directory_request.sensitivity)
        try:
            directory = _resolve_allowed_personal_dir(directory)
            
            # Security check - ensure directory exists and is accessible
            if not os.path.exists(directory):
                raise HTTPException(404, f"Directory not found: {directory}")
            
            if not os.path.isdir(directory):
                raise HTTPException(400, f"Path is not a directory: {directory}")
            
            logger.info(f"Adding directory to RAG: {directory}")
            
            # Use the RAGManager to index the directory
            rag = _rag()
            if rag:
                def _index_directory():
                    result = rag.index_personal_documents(
                        directory, owner=owner, sensitivity=sensitivity
                    )
                    if result["success"]:
                        # Also update the personal_docs_manager to track this
                        # directory, with the same label as the chunks just
                        # written so the tracker agrees with the index. Indexing
                        # already happened, hence index=False. Kept inside the
                        # offloaded call: it triggers refresh_index(), which
                        # re-extracts text across tracked directories.
                        personal_docs_manager.add_directory(
                            directory, index=False, sensitivity=sensitivity
                        )
                    return result

                # Indexing walks, embeds, and stores the whole tree -- minutes
                # on a real directory. The handler is async, so calling it
                # inline runs it on the event loop and every other request
                # queues behind it until it finishes (#5558). Serialize on the
                # async job lock BEFORE offloading so a queued request parks on
                # the loop instead of pinning a threadpool worker.
                async with _index_job_lock:
                    result = await run_in_threadpool(_index_directory)

                if result["success"]:
                    return {
                        "success": True,
                        "message": f"Successfully indexed {result['indexed_count']} chunks from {directory}",
                        "indexed_count": result["indexed_count"],
                        "failed_count": result.get("failed_count", 0),
                        "directory": directory,
                        "sensitivity": sensitivity,
                    }
                else:
                    raise HTTPException(500, result.get("message", "Failed to index directory"))
            else:
                raise HTTPException(503, "RAG system is not available")
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding directory to RAG: {e}")
            raise HTTPException(500, f"Failed to add directory: {str(e)}")
    
    @router.post("/scan")
    async def scan_indexed_directories(
        owner: str = Depends(require_user), _admin: None = Depends(require_admin),
    ):
        """Re-index files changed since the last scan.

        The same incremental pass the background scanner runs; exposed so a save
        can be picked up immediately instead of waiting for the next tick.
        """
        rag = _rag()
        if not rag:
            raise HTTPException(503, "RAG system is not available")

        from src.vault_scan import VaultScanner

        try:
            scanner = VaultScanner(personal_docs_manager, rag)
            result = scanner.scan()
        except Exception as e:
            logger.error(f"Vault scan failed: {e}")
            raise HTTPException(500, f"Scan failed: {str(e)}")

        return {"success": True, **result}

    @router.post("/directory_sensitivity")
    async def set_directory_sensitivity(
        directory_request: DirectoryRequest,
        owner: str = Depends(require_user), _admin: None = Depends(require_admin),
    ):
        """Relabel a tracked directory as public or private.

        Also rewrites the label on chunks already in the vector store, so
        marking a directory private takes effect for content indexed earlier.
        """
        if directory_request.sensitivity is None:
            raise HTTPException(400, "sensitivity is required ('public' or 'private')")

        directory = _resolve_allowed_personal_dir(directory_request.directory)
        sensitivity = normalize_sensitivity(directory_request.sensitivity)

        if not hasattr(personal_docs_manager, "set_directory_sensitivity"):
            raise HTTPException(503, "Sensitivity labelling is not available")

        try:
            result = personal_docs_manager.set_directory_sensitivity(directory, sensitivity)
        except Exception as e:
            logger.error(f"Error relabelling directory {directory}: {e}")
            raise HTTPException(500, f"Failed to set sensitivity: {str(e)}")

        return {
            "success": True,
            "directory": directory,
            "sensitivity": result.get("sensitivity", sensitivity),
            "updated_count": result.get("updated_count", 0),
            "message": (
                f"{directory} is now {result.get('sensitivity', sensitivity)}; "
                f"{result.get('updated_count', 0)} indexed chunk(s) relabelled"
            ),
        }

    @router.delete("/remove_directory")
    async def remove_directory_from_rag(directory: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """
        Remove a directory from the RAG index.

        Args:
            directory: Path to the directory to remove

        Returns:
            JSON response confirming removal
        """
        try:
            # Confine to PERSONAL_DIR — parity with add_directory_to_rag (which
            # resolves the path the same way). Without this, an arbitrary or
            # `..`-escaping path is passed straight to
            # personal_docs_manager.remove_directory / rag.remove_directory.
            directory = _resolve_allowed_personal_dir(directory)

            logger.info(f"Removing directory from RAG: {directory}")

            rag = _rag()

            def _remove_directory():
                # Always remove from personal_docs_manager tracking. This
                # mutates the same unsynchronized list/index an add job touches
                # and re-extracts text (refresh_index), so it is blocking work.
                if hasattr(personal_docs_manager, 'remove_directory'):
                    personal_docs_manager.remove_directory(directory)
                # Remove from RAG vector store (best-effort).
                if rag:
                    try:
                        rag.remove_directory(directory)
                    except Exception as e:
                        logger.warning(f"RAG removal failed for directory {directory}: {e}")

            # Same job lock as add/reload so remove cannot interleave with an
            # in-flight add; offloaded off the event loop.
            async with _index_job_lock:
                await run_in_threadpool(_remove_directory)

            return {
                "success": True,
                "message": f"Successfully removed {directory} from RAG index",
                "directory": directory
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing directory from RAG: {e}")
            raise HTTPException(500, f"Failed to remove directory: {str(e)}")
    
    @router.post("/upload")
    async def upload_files_to_rag(request: Request, files: List[UploadFile] = File(...)):
        """Upload files directly into RAG. Supports text and PDF."""
        user = require_privilege(request, "can_use_documents")
        rag = _rag()
        if not rag:
            raise HTTPException(503, "RAG system is not available — is the embedding service running?")

        upload_dir = _personal_upload_dir_for_owner(user)

        total_indexed = 0
        total_failed = 0
        uploaded_files = []

        # Chunking, embedding and the tracking update are blocking work over the
        # same vector/tracking state add_directory mutates (#5634). Take the
        # shared job lock BEFORE offloading so a queued request parks on the loop
        # instead of pinning a threadpool worker, matching add_directory.
        # Read and process one capped payload at a time so a multi-file request
        # cannot retain len(files) * PERSONAL_UPLOAD_MAX_BYTES in memory.
        async with _index_job_lock:
            for upload in files:
                try:
                    file_path, stored_name, safe_name = _unique_personal_upload_path(
                        upload_dir, upload.filename
                    )
                    content_bytes = await upload.read(PERSONAL_UPLOAD_MAX_BYTES + 1)
                    if len(content_bytes) > PERSONAL_UPLOAD_MAX_BYTES:
                        logger.warning(f"Rejected oversized personal upload: {upload.filename!r}")
                        total_failed += 1
                        continue

                    def _index_upload():
                        with open(file_path, "wb") as f:
                            f.write(content_bytes)

                        ext = os.path.splitext(safe_name)[1].lower()
                        if ext == ".pdf":
                            from src.personal_docs import extract_pdf_text
                            text = extract_pdf_text(file_path)
                        else:
                            text = content_bytes.decode("utf-8", errors="replace")

                        if not text or not text.strip():
                            return 0, 1, None

                        indexed = 0
                        failed = 0
                        chunks = rag._split_into_chunks(text, chunk_size=500)
                        for i, chunk in enumerate(chunks):
                            metadata = {
                                "source": file_path,
                                "filename": safe_name,
                                "stored_filename": stored_name,
                                "directory": upload_dir,
                                "type": ext,
                                "chunk_id": i,
                            }
                            if user:
                                metadata["owner"] = user
                            if rag.add_document(chunk, metadata):
                                indexed += 1
                            else:
                                failed += 1
                        return indexed, failed, safe_name

                    indexed, failed, uploaded_name = await run_in_threadpool(_index_upload)
                    total_indexed += indexed
                    total_failed += failed
                    if uploaded_name:
                        uploaded_files.append(uploaded_name)
                except Exception as e:
                    logger.error(f"Failed to upload/index {upload.filename}: {e}")
                    total_failed += 1

            # Same transition, same lock: the tracking update must not land
            # while another job is mid-write over the same state.
            if uploaded_files and hasattr(personal_docs_manager, "add_directory"):
                await run_in_threadpool(
                    personal_docs_manager.add_directory, upload_dir, index=False
                )

        return {
            "success": True,
            "uploaded": uploaded_files,
            "indexed_count": total_indexed,
            "failed_count": total_failed,
        }

    @router.delete("/file")
    async def delete_file_from_rag(filepath: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Delete a specific file from RAG index and optionally from disk."""
        try:
            def _delete_file():
                # Remove chunks from RAG vector store (best-effort)
                removed = 0
                rag = _rag()
                if rag:
                    try:
                        removed = rag.delete_by_source(filepath)
                    except Exception as e:
                        logger.warning(f"RAG removal failed for {filepath}: {e}")

                # Delete file from disk if it's in the caller's own uploads dir.
                # Scope to the per-owner subdir, not the shared uploads root, so one
                # admin can't delete another user's personal files by path.
                deleted_from_disk = False
                try:
                    abs_target = os.path.realpath(filepath)
                    base_abs = os.path.realpath(_personal_upload_dir_for_owner(owner, create=False))
                    in_uploads = (
                        abs_target == base_abs
                        or os.path.commonpath([abs_target, base_abs]) == base_abs
                    )
                except ValueError:
                    # commonpath raises on mixed drives / non-comparable paths
                    in_uploads = False
                if in_uploads and abs_target != base_abs:
                    try:
                        os.remove(abs_target)
                        deleted_from_disk = True
                    except FileNotFoundError:
                        pass  # already gone — race with another request or cleanup

                # Exclude the file from the listing (persists across restarts)
                personal_docs_manager.exclude_file(filepath)
                return removed, deleted_from_disk

            # Vector removal, the disk unlink and the exclusion write are one
            # transition over the same state add_directory mutates (#5634), and
            # all three block. Take the shared job lock BEFORE offloading, as
            # add_directory does.
            async with _index_job_lock:
                removed, deleted_from_disk = await run_in_threadpool(_delete_file)

            return {
                "success": True,
                "removed_chunks": removed,
                "deleted_from_disk": deleted_from_disk,
            }
        except Exception as e:
            logger.error(f"Failed to delete file {filepath}: {e}")
            raise HTTPException(500, f"Failed to delete file: {str(e)}")

    return router
