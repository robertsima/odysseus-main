# src/app_initializer.py
"""Initialize all application components and dependencies."""
import os
import logging
from typing import Dict, Any

from src.constants import (
    DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR,
    SESSIONS_FILE, DEFAULT_HOST, OPENAI_API_KEY
)
from src.memory import MemoryManager
from src.memory_provider import MemoryProviderRegistry, NativeMemoryProvider
from services.memory.skills import SkillsManager
from core.session_manager import SessionManager
from core.models import set_session_manager
from src.personal_docs import PersonalDocsManager
from src.api_key_manager import APIKeyManager
from src.preset_manager import PresetManager
from src.chat_processor import ChatProcessor
from src.model_discovery import ModelDiscovery
from src.chat_handler import ChatHandler
from src.research_handler import ResearchHandler
from src.upload_handler import UploadHandler
from src.tool_utils import set_upload_handler
from src.search import update_search_config
from src.builtin_skills import seed_bundled_skills

logger = logging.getLogger(__name__)

def create_directories():
    """Create necessary directories if they don't exist."""
    for directory in (DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR):
        os.makedirs(directory, exist_ok=True)
        
def initialize_managers(base_dir: str, rag_manager=None) -> Dict[str, Any]:
    """
    Initialize all manager and handler instances.

    Args:
        base_dir: Base directory path
        rag_manager: RAG manager instance (optional)
    Returns:
        Dictionary containing all initialized components
    """
    # Create directories first
    create_directories()

    # Initialize core managers
    memory_manager = MemoryManager(DATA_DIR)
    skills_manager = SkillsManager(DATA_DIR)
    try:
        seed_bundled_skills(skills_manager)
    except Exception as e:  # never let a bundled skill block startup
        logger.warning(f"Bundled skill installation failed: {e}")
    session_manager = SessionManager(SESSIONS_FILE)
    set_session_manager(session_manager)  # Enable Session.add_message() persistence
    upload_handler = UploadHandler(base_dir, UPLOAD_DIR)
    session_manager.upload_handler = upload_handler
    set_upload_handler(upload_handler)
    personal_docs_manager = PersonalDocsManager(PERSONAL_DIR, rag_manager)
    # Apply ODYSSEUS_PERSONAL_DIRS before anything can retrieve: a declared
    # private tree must carry its label from the first request, not from
    # whenever an operator remembers to set it.
    try:
        from src.personal_dirs_config import reconcile as reconcile_personal_dirs
        reconcile_personal_dirs(personal_docs_manager)
    except Exception as e:  # never let declarative config block startup
        logger.error(f"Personal directory reconciliation failed: {e}")
    # Indexing is one-shot and tracked in JSON, so an empty/reset vector store
    # would otherwise stay empty forever while the app believed the vault was
    # indexed — and every chat turn would silently retrieve nothing.
    try:
        personal_docs_manager.reindex_if_empty()
    except Exception as e:
        logger.error(f"Document re-index check failed: {e}")
    api_key_manager = APIKeyManager(DATA_DIR)
    preset_manager = PresetManager(DATA_DIR)

    # Initialize memory vector store (share embedding model with RAG if available)
    memory_vector = None
    try:
        from src.memory_vector import MemoryVectorStore
        embedding_model = getattr(rag_manager, '_model', None) if rag_manager else None
        memory_vector = MemoryVectorStore(DATA_DIR, embedding_model=embedding_model)
        if memory_vector.healthy:
            # Rebuild index from existing memories if empty
            if memory_vector.count() == 0:
                existing = memory_manager.load()
                if existing:
                    memory_vector.rebuild(existing)
                    logger.info(f"Rebuilt memory vector index from {len(existing)} existing entries")
            logger.info("MemoryVectorStore initialized")
        else:
            # Keep the unhealthy object (do NOT reset to None): consumers gate on
            # `.healthy`, and service_health.chromadb_health() needs a present
            # object to report DEGRADED/DOWN instead of DISABLED ("not configured").
            logger.warning("MemoryVectorStore DEGRADED: ChromaDB vector memory unavailable")
    except Exception as e:
        logger.warning(f"MemoryVectorStore DEGRADED: {e}")
        memory_vector = None

    memory_provider_registry = MemoryProviderRegistry([
        NativeMemoryProvider(memory_manager, memory_vector),
    ])

    # Initialize processors
    chat_processor = ChatProcessor(memory_manager, personal_docs_manager, memory_vector=memory_vector, skills_manager=skills_manager)
    research_handler = ResearchHandler()
    
    # Initialize chat handler with all dependencies
    chat_handler = ChatHandler(
        session_manager=session_manager,
        memory_manager=memory_manager,
        chat_processor=chat_processor,
        research_handler=research_handler,
        preset_manager=preset_manager,
        upload_handler=upload_handler,
    )
    
    # Initialize model discovery
    model_discovery = ModelDiscovery(DEFAULT_HOST, OPENAI_API_KEY)
    
    # Load and apply saved API keys
    saved_keys = api_key_manager.load()
    if "brave" in saved_keys:
        update_search_config(api_key=saved_keys["brave"])
        logger.info("Loaded Brave API key from saved configuration")
    
    return {
        "memory_manager": memory_manager,
        "memory_vector": memory_vector,
        "memory_provider_registry": memory_provider_registry,
        "skills_manager": skills_manager,
        "session_manager": session_manager,
        "upload_handler": upload_handler,
        "personal_docs_manager": personal_docs_manager,
        "api_key_manager": api_key_manager,
        "preset_manager": preset_manager,
        "chat_processor": chat_processor,
        "research_handler": research_handler,
        "chat_handler": chat_handler,
        "model_discovery": model_discovery,
        "current_presets": preset_manager.presets,
        "PERSONAL_INDEX": personal_docs_manager.index
    }
