"""JARVIS API entry point."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.ai.provider_factory import provider_status
from app.ai.speech import speech_status
from app.config import settings
from app.routers import assistant, auth, conversations, devices, memory, sync, tasks, voice

if settings.app_env == "production" and len(settings.jwt_secret) < 32:
    raise RuntimeError("JWT_SECRET must be at least 32 characters in production.")

app = FastAPI(title="JARVIS", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(memory.router)
app.include_router(tasks.router)
app.include_router(assistant.router)
app.include_router(devices.router)
app.include_router(sync.router)
app.include_router(voice.router)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"error": {"code": "error", "message": str(exc.detail)}}
    )
    return JSONResponse(status_code=exc.status_code, content=detail)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = sorted({"".join(str(part) for part in err["loc"]) for err in exc.errors()})
    return JSONResponse(
        status_code=422,
        content={
            "error": {"code": "invalid_request", "message": "Invalid fields: " + ", ".join(fields)}
        },
    )


@app.get("/api/v1/health")
async def health() -> dict:
    return {"status": "ok", "ai": provider_status(), "speech": speech_status()}
