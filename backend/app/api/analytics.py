"""Analytics router — dashboard summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_analytics_service
from app.schemas.analytics import AnalyticsDashboard
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("/dashboard", response_model=AnalyticsDashboard)
async def dashboard(
    service: AnalyticsService = Depends(get_analytics_service),
) -> AnalyticsDashboard:
    return await service.dashboard()
