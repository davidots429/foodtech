from fastapi import FastAPI

from .auth import router as auth_router
from .ai import router as ai_router
from .cooking import router as cooking_router
from .credentials import router as credentials_router
from .tier import router as tier_router

app = FastAPI(title="Foodtech Business API")
app.include_router(auth_router)
app.include_router(ai_router)
app.include_router(cooking_router)
app.include_router(credentials_router)
app.include_router(tier_router)

