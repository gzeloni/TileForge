"""API routers."""

from fastapi import APIRouter

from app.routes.maps import router as maps_router

router = APIRouter()
router.include_router(maps_router)
