from fastapi import FastAPI

from .auth import router as auth_router
from .cooking import router as cooking_router

app = FastAPI(title="Foodtech Business API")
app.include_router(auth_router)
app.include_router(cooking_router)

