from fastapi import FastAPI

from .auth import router as auth_router

app = FastAPI(title="Foodtech Business API")
app.include_router(auth_router)

