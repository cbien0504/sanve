import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import ResourcesDep

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness: the process is up. Deliberately does not check dependencies, otherwise a database
    outage would make Kubernetes restart every healthy pod."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(resources: ResourcesDep) -> JSONResponse:
    """Readiness: can this replica serve traffic right now? If not, the load balancer skips it."""

    async def check_db() -> None:
        async with resources.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    checks = {"database": check_db(), "redis": resources.redis.ping()}
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            await asyncio.wait_for(check, timeout=1.0)
            results[name] = "ok"
        except Exception as exc:
            results[name] = f"error: {type(exc).__name__}"
    ready = all(value == "ok" for value in results.values())
    return JSONResponse(
        {"status": "ok" if ready else "unavailable", "checks": results}, status_code=200 if ready else 503
    )
