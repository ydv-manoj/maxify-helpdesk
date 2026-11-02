import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.api.routes import router  # noqa: E402  (after env is loaded)
from app.db.models import Customer  # noqa: E402
from app.db.seed import seed  # noqa: E402
from app.db.session import Base, SessionLocal, engine  # noqa: E402
from app.llm.decider import get_decider  # noqa: E402
from app.llm.factory import get_llm  # noqa: E402

import os  # noqa: E402

app = FastAPI(title="Threadline Support Agent")
lab_llm = get_llm("mock")  # Scenario lab: deterministic
lab_decider = get_decider("rules")
live_decider = get_decider(os.getenv("DECIDER_PROVIDER", "jev"))
live_llm = get_llm(os.getenv("LIVE_LLM_PROVIDER", "gemini"), os.getenv("LIVE_LLM_MODEL", "gemini-3.5-flash-lite"))

Base.metadata.create_all(engine)
with SessionLocal() as _db:
    if _db.query(Customer).count() == 0:
        seed()

app.include_router(router)
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/desk")
def desk():
    return FileResponse(STATIC / "desk.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "lab": {"provider": lab_llm.provider, "model": lab_llm.model},
        "live": {"provider": live_llm.provider, "model": live_llm.model,
                 "available": live_llm.provider != "mock", "unavailable_reason": live_llm.unavailable_reason,
                 "decider": {"provider": live_decider.provider, "model": live_decider.model}},
    }
