"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

实体复核候选集专用路由含固定路径，需在通用资源动态路由之前注册，
否则 /api/entity_recognize/review-candidates 会被 /api/{module} 抢先匹配。
"""
from __future__ import annotations

from app.routers import entity_review, resource

ROUTERS = [entity_review, resource]
