import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from core import (
    BUILTIN_LORA_DIR,
    CLONE_VOICES_DIR,
    DATASET_BUILDER_DIR,
    DESIGNED_VOICES_DIR,
    LORA_MODELS_DIR,
    STATIC_DIR,
    VOICELINES_DIR,
    project_manager,
)
from utils import check_basic_auth


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AlexandriaUI")


def reset_stuck_chunks():
    """Reset chunks left generating by a prior interrupted server process."""
    chunks = project_manager.load_chunks()
    if not chunks:
        return 0

    reset_count = 0
    for chunk in chunks:
        if chunk.get("status") == "generating":
            chunk["status"] = "pending"
            reset_count += 1
    if reset_count:
        project_manager.save_chunks(chunks)
        print(f"Startup: reset {reset_count} stuck 'generating' chunk(s) to 'pending'")
    return reset_count


@asynccontextmanager
async def lifespan(_app):
    reset_stuck_chunks()
    yield


app = FastAPI(title="Alexandria Audiobook", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Create voicelines directory if it doesn't exist to prevent startup error
app.mount("/voicelines", StaticFiles(directory=VOICELINES_DIR), name="voicelines")

# Designed voices directory for voice designer feature
app.mount("/designed_voices", StaticFiles(directory=DESIGNED_VOICES_DIR), name="designed_voices")

# Clone voices directory for user-uploaded reference audio
app.mount("/clone_voices", StaticFiles(directory=CLONE_VOICES_DIR), name="clone_voices")

app.mount("/lora_models", StaticFiles(directory=LORA_MODELS_DIR), name="lora_models")

# Built-in LoRA adapters directory
app.mount("/builtin_lora", StaticFiles(directory=BUILTIN_LORA_DIR), name="builtin_lora")

# Dataset builder directory for preview audio
app.mount("/dataset_builder", StaticFiles(directory=DATASET_BUILDER_DIR), name="dataset_builder")

# CORS — allow configurable origins via env var, defaulting to localhost for security
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://127.0.0.1:4200,http://localhost:4200").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional HTTP Basic Auth gate. OFF by default: only registered when
# ALEXANDRIA_AUTH_PASSWORD is set, so the local Pinokio flow is unchanged and
# pays no per-request cost. When enabled, every request must carry valid Basic
# credentials — the browser stores them once (native dialog) and re-sends on
# fetch, download links, and <audio> loads alike. Set this before exposing the
# app beyond localhost (e.g. the Docker image binds 0.0.0.0).
_AUTH_USERNAME = os.environ.get("ALEXANDRIA_AUTH_USERNAME", "alexandria")
_AUTH_PASSWORD = os.environ.get("ALEXANDRIA_AUTH_PASSWORD", "")
if _AUTH_PASSWORD:
    from starlette.responses import Response as _StarletteResponse

    @app.middleware("http")
    async def _basic_auth_gate(request, call_next):
        # CORS preflight carries no credentials by design; let it through so the
        # CORS middleware can answer it.
        if request.method == "OPTIONS":
            return await call_next(request)
        if check_basic_auth(request.headers.get("Authorization", ""),
                             _AUTH_USERNAME, _AUTH_PASSWORD):
            return await call_next(request)
        return _StarletteResponse(
            "Authentication required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Alexandria"'})
    print("Auth: HTTP Basic Auth enabled (ALEXANDRIA_AUTH_PASSWORD is set)")

from routers.system import router as system_router

app.include_router(system_router)

from routers.script import router as script_router

app.include_router(script_router)

from routers.voices import router as voices_router

app.include_router(voices_router)

from routers.editor import router as editor_router

app.include_router(editor_router)

from routers.scripts_library import router as scripts_library_router

app.include_router(scripts_library_router)

from routers.voice_library import router as voice_library_router

app.include_router(voice_library_router)

from routers.voice_design import router as voice_design_router

app.include_router(voice_design_router)

from routers.lora import router as lora_router

app.include_router(lora_router)

from routers.dataset_builder import router as dataset_builder_router

app.include_router(dataset_builder_router)

from routers.preparer import router as preparer_router

app.include_router(preparer_router)

from routers.voicelab import router as voicelab_router

app.include_router(voicelab_router)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("ALEXANDRIA_HOST", "127.0.0.1")
    port = int(os.environ.get("ALEXANDRIA_PORT", "4200"))
    uvicorn.run(app, host=host, port=port, access_log=False)
