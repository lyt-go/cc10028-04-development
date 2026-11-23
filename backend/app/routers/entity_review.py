"""实体复核候选集路由：候选集构建/查询、历史 run 对比、候选派单与结案依据查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.annot_review import AnnotReviewService
from app.services.entity_recognize import EntityRecognizeService

router = APIRouter(prefix="/api", tags=["实体复核候选集"])


class BuildCandidatesPayload(BaseModel):
    chunk_set_id: str | None = None
    thresholds: dict[str, float] | None = None


class DispatchPayload(BaseModel):
    batch_key: str
    candidate_ids: list[str] | None = None
    run_id: str | None = None
    reviewer: str = ""


class CandidateConclusionPayload(BaseModel):
    conclusion: str
    reviewer: str
    covered_evidence_ids: list[str] | None = None


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/entity_recognize/review-candidates/build")
def build_candidates(payload: BuildCandidatesPayload) -> dict[str, Any]:
    """按类别置信度阈值从命名实体数据构建复核候选集，生成不可变 run 快照。"""
    return _unwrap(EntityRecognizeService().build_review_candidates(
        chunk_set_id=payload.chunk_set_id,
        thresholds=payload.thresholds,
    ))


@router.get("/entity_recognize/review-candidates")
def list_candidates(
    run_id: str | None = None,
    entity_type: str | None = None,
    only_cross: bool = False,
) -> dict[str, Any]:
    """读取候选集；不传 run_id 时返回最新一次构建，传入 run_id 返回该次历史快照。"""
    items = EntityRecognizeService().list_review_candidates(
        run_id=run_id, entity_type=entity_type, only_cross=only_cross)
    return {"items": items, "total": len(items), "run_id": run_id or "latest"}


@router.get("/entity_recognize/review-candidates/runs/history")
def candidate_run_history() -> dict[str, Any]:
    """历次构建记录：改阈值后据此对比历史数据、适用阈值与旧结论。"""
    items = EntityRecognizeService().list_candidate_runs()
    return {"items": items, "total": len(items)}


@router.get("/entity_recognize/review-candidates/{candidate_id}")
def get_candidate(candidate_id: str) -> dict[str, Any]:
    """单个候选详情：当前判定 + 各次 run 的阈值历史与状态。"""
    result = EntityRecognizeService().get_review_candidate(candidate_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"复核候选 {candidate_id} 不存在")
    return result


@router.post("/annot_review/candidate-dispatch")
def dispatch_candidate_todos(payload: DispatchPayload) -> dict[str, Any]:
    """用复核候选集创建人工复核待办，返回每个样本采用的规则和被选中的来源。"""
    return _unwrap(AnnotReviewService().create_review_todos_from_candidates(
        batch_key=payload.batch_key,
        candidate_ids=payload.candidate_ids,
        run_id=payload.run_id,
        reviewer=payload.reviewer,
    ))


@router.post("/annot_review/reviews/{review_id}/candidate-conclusion")
def submit_candidate_conclusion(review_id: str, payload: CandidateConclusionPayload) -> dict[str, Any]:
    """登记候选复核结论；跨阈值候选未覆盖全部证据时拒绝单独结案。"""
    return _unwrap(AnnotReviewService().submit_candidate_review(
        review_id=review_id,
        conclusion=payload.conclusion,
        reviewer=payload.reviewer,
        covered_evidence_ids=payload.covered_evidence_ids,
    ))


@router.get("/annot_review/closed-reviews")
def query_closed_reviews(
    candidate_id: str | None = None,
    run_id: str | None = None,
    chunk_id: str | None = None,
) -> dict[str, Any]:
    """按原依据（候选/run/分块出处）查询已结案结果及其采用的规则与来源。"""
    items = AnnotReviewService().query_closed_reviews(
        candidate_id=candidate_id, run_id=run_id, chunk_id=chunk_id)
    return {"items": items, "total": len(items)}
