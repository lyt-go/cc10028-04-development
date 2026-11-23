"""实体复核候选集集成测试。

场景主线（对应用户验收口径）：
- 跨阈值机构「华远科技集团」在标题、正文各出现一次，两条出处置信度恰好分列
  类别阈值两侧；
- 只出现一次的机构「单次出现的凌云制造公司」仅在正文出现且低于阈值；
- 改阈值重跑后历史 run/旧结论仍可按原依据查询，同一证据同一规则不重复派单；
- 实体名相同也保留真实分块出处。
"""
from __future__ import annotations

import uuid as _uuid

import pytest
from fastapi import HTTPException
from app.routers import entity_review as review_router
from app.routers.entity_review import (
    BuildCandidatesPayload,
    CandidateConclusionPayload,
    DispatchPayload,
)
from app.services.base import DATA_ROOT
from app.services.entity_recognize import EntityRecognizeService
from app.services.annot_review import AnnotReviewService


CROSS_ORG = "华远科技集团"
SINGLE_ORG = "凌云制造公司"
ORG_THRESHOLD = EntityRecognizeService.DEFAULT_TYPE_THRESHOLDS["ORG"]  # 0.85


def _uid(prefix="SET"):
    return f"{prefix}-{_uuid.uuid4().hex[:10].upper()}"


def _write_chunks(chunk_set_id: str) -> str:
    """构造标题/正文两个分块：跨阈值机构各出现一次，单次机构只在正文出现。"""
    chunks = [
        {
            "chunk_id": f"CHUNK-{chunk_set_id}-0001",
            "doc_id": chunk_set_id,
            "page": 1,
            "seq": 1,
            "section": "标题",
            "text": f"{CROSS_ORG}发布年度技术战略",
            "entity_confidence": {CROSS_ORG: 0.86},
        },
        {
            "chunk_id": f"CHUNK-{chunk_set_id}-0002",
            "doc_id": chunk_set_id,
            "page": 2,
            "seq": 2,
            "section": "正文",
            "text": f"正文段落提到{CROSS_ORG}与{SINGLE_ORG}签署合作协议。",
            "entity_confidence": {CROSS_ORG: 0.84, SINGLE_ORG: 0.80},
        },
    ]
    path = DATA_ROOT / "chunks" / f"{chunk_set_id}_chunks.jsonl"
    EntityRecognizeService()._overwrite_jsonl(path, chunks)
    return chunk_set_id


def _recognize(chunk_set_id: str) -> list[dict]:
    result = EntityRecognizeService().recognize_from_chunk(chunk_set_id)
    assert "error" not in result, result
    return EntityRecognizeService()._read_jsonl(
        DATA_ROOT / "entities" / f"{chunk_set_id}_entities.jsonl")


class TestCandidateBuildAndThresholds:
    """候选集构建：按类别阈值判定、记录适用阈值与抽取出处。"""

    def test_candidate_set_applies_per_type_threshold(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        result = svc.build_review_candidates(chunk_set_id=set_id)
        assert "error" not in result
        by_text = {c["entity_text"]: c for c in result["candidates"]}
        # 两个机构都含低于 ORG 阈值的出处，均进入候选集
        assert CROSS_ORG in by_text and SINGLE_ORG in by_text
        cross = by_text[CROSS_ORG]
        assert cross["applied_threshold"] == ORG_THRESHOLD
        assert cross["threshold_rule"]["entity_type"] == "ORG"
        # 两条出处的置信度恰好跨过阈值（0.86 >= 0.85 > 0.84）
        sides = sorted(e["threshold_side"] for e in cross["evidence"])
        assert sides == ["above", "below"]
        assert cross["cross_threshold"] is True
        assert cross["independently_closable"] is False

    def test_candidate_records_rule_and_real_provenance(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        svc.build_review_candidates(chunk_set_id=set_id)
        candidates = svc.list_review_candidates()
        cross = next(c for c in candidates if c["entity_text"] == CROSS_ORG)
        assert cross["sections"] == ["标题", "正文"]
        chunk_ids = {e["chunk_id"] for e in cross["evidence"]}
        assert len(chunk_ids) == 2, "同名实体必须保留两个真实分块出处"
        for evidence in cross["evidence"]:
            assert evidence["applied_threshold"] == ORG_THRESHOLD
            assert evidence["source_file"].endswith("_entities.jsonl")
            assert evidence["source_excerpt"], "抽取出处需带原文片段"
            assert evidence["evidence_id"].startswith("EVD-")

    def test_same_name_keeps_distinct_chunk_provenance(self):
        set_id = _write_chunks(_uid())
        entities = _recognize(set_id)
        same = [e for e in entities if e["entity_text"] == CROSS_ORG]
        assert len(same) == 2
        assert {e["section"] for e in same} == {"标题", "正文"}
        assert {e["chunk_id"] for e in same} != {same[0]["chunk_id"]}

    def test_single_occurrence_org_is_closable(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        svc.build_review_candidates(chunk_set_id=set_id)
        single = next(c for c in svc.list_review_candidates()
                      if c["entity_text"] == SINGLE_ORG)
        assert single["evidence_count"] == 1
        assert single["cross_threshold"] is False
        assert single["independently_closable"] is True

    def test_threshold_override_changes_candidate_membership(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        first = svc.build_review_candidates(chunk_set_id=set_id)
        first_texts = {c["entity_text"] for c in first["candidates"]}
        # 提高 ORG 阈值后，标题处 0.86 也变为低分，跨阈值候选变为全部低分
        second = svc.build_review_candidates(
            chunk_set_id=set_id, thresholds={"ORG": 0.90})
        cross = next(c for c in second["candidates"] if c["entity_text"] == CROSS_ORG)
        assert cross["applied_threshold"] == 0.90
        assert cross["cross_threshold"] is False
        assert cross["below_count"] == 2
        # 历史 run 与最新快照都可查询
        assert first["run_id"] != second["run_id"]
        history = svc.list_review_candidates(run_id=first["run_id"])
        assert next(c for c in history if c["entity_text"] == CROSS_ORG)["cross_threshold"] is True
        assert first_texts  # 第一次构建结果未被覆盖


class TestDispatchAndIdempotency:
    """候选派单：保留规则与来源，同证据同规则重跑不重复派单。"""

    def test_dispatch_preserves_rule_and_selected_sources(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        run = svc.build_review_candidates(chunk_set_id=set_id)
        review_svc = AnnotReviewService()
        batch = _uid("BATCH")
        result = review_svc.create_review_todos_from_candidates(batch_key=batch)
        assert result["tasks_created"] >= 2
        cross_task = next(t for t in result["dispatched_tasks"]
                          if CROSS_ORG in t["复核对象"])
        assert cross_task["threshold_rule"]["threshold"] == ORG_THRESHOLD
        assert cross_task["cross_threshold"] is True
        # 只对低分证据派单，但全部证据（含高分标题来源）随任务保留
        assert len(cross_task["selected_sources"]) == 1
        assert cross_task["selected_sources"][0]["section"] == "正文"
        assert {e["section"] for e in cross_task["all_evidence"]} == {"标题", "正文"}
        assert cross_task["run_id"] == run["run_id"]

    def test_same_evidence_same_rule_no_duplicate_dispatch(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        svc.build_review_candidates(chunk_set_id=set_id)
        review_svc = AnnotReviewService()
        batch = _uid("BATCH")
        first = review_svc.create_review_todos_from_candidates(batch_key=batch)
        assert first["tasks_created"] >= 2
        second = review_svc.create_review_todos_from_candidates(batch_key=batch)
        assert second["tasks_created"] == 0
        assert len(second["skipped_dispatched"]) > 0

    def test_changed_threshold_redispatches_only_under_new_rule(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        svc = EntityRecognizeService()
        svc.build_review_candidates(chunk_set_id=set_id)
        review_svc = AnnotReviewService()
        batch = _uid("BATCH")
        review_svc.create_review_todos_from_candidates(batch_key=batch)
        # 改阈值重建：标题证据在新规则下首次低于阈值，应产生新派单
        svc.build_review_candidates(chunk_set_id=set_id, thresholds={"ORG": 0.90})
        rerun = review_svc.create_review_todos_from_candidates(batch_key=batch)
        cross_task = next(t for t in rerun["dispatched_tasks"]
                          if CROSS_ORG in t["复核对象"])
        sections = {s["section"] for s in cross_task["selected_sources"]}
        assert "标题" in sections
        assert cross_task["threshold_rule"]["threshold"] == 0.90


class TestConclusionAndHistory:
    """结案约束与历史结论可查询。"""

    def _one_task(self, batch: str) -> dict:
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        EntityRecognizeService().build_review_candidates(chunk_set_id=set_id)
        review_svc = AnnotReviewService()
        result = review_svc.create_review_todos_from_candidates(batch_key=batch)
        return result

    def test_cross_threshold_candidate_cannot_close_alone(self):
        batch = _uid("BATCH")
        dispatch = self._one_task(batch)
        cross_task = next(t for t in dispatch["dispatched_tasks"]
                          if CROSS_ORG in t["复核对象"])
        review_svc = AnnotReviewService()
        # 只覆盖正文低分证据时拒绝结案
        blocked = review_svc.submit_candidate_review(
            cross_task["复核编号"], "仅核对正文", "复核员A",
            covered_evidence_ids=[cross_task["selected_sources"][0]["evidence_id"]])
        assert "error" in blocked
        assert blocked["missing_evidence"]
        # 覆盖标题+正文全部证据后允许结案
        all_ids = [e["evidence_id"] for e in cross_task["all_evidence"]]
        ok = review_svc.submit_candidate_review(
            cross_task["复核编号"], "两条证据口径一致，确认实体有效",
            "复核员A", covered_evidence_ids=all_ids)
        assert ok["status"] == "已结案"
        assert set(ok["covered_evidence"]) == set(all_ids)
        assert ok["threshold_rule"]["threshold"] == ORG_THRESHOLD

    def test_closed_review_immutable_and_queryable_by_original_basis(self):
        batch = _uid("BATCH")
        dispatch = self._one_task(batch)
        cross_task = next(t for t in dispatch["dispatched_tasks"]
                          if CROSS_ORG in t["复核对象"])
        run_id = cross_task["run_id"]
        review_svc = AnnotReviewService()
        all_ids = [e["evidence_id"] for e in cross_task["all_evidence"]]
        review_svc.submit_candidate_review(
            cross_task["复核编号"], "确认覆盖两条证据", "复核员B",
            covered_evidence_ids=all_ids)
        # 已结案不可再改
        again = review_svc.submit_candidate_review(
            cross_task["复核编号"], "试图改结论", "复核员B",
            covered_evidence_ids=all_ids)
        assert "error" in again
        # 改阈值重跑候选集
        svc = EntityRecognizeService()
        svc.build_review_candidates(thresholds={"ORG": 0.90})
        # 仍可按候选、run、分块三种原依据查到旧结论
        by_candidate = review_svc.query_closed_reviews(candidate_id=cross_task["candidate_id"])
        by_run = review_svc.query_closed_reviews(run_id=run_id)
        title_chunk = next(e["chunk_id"] for e in cross_task["all_evidence"]
                           if e["section"] == "标题")
        by_chunk = review_svc.query_closed_reviews(chunk_id=title_chunk)
        assert by_candidate and by_run and by_chunk
        closed = by_candidate[0]
        assert closed["复核结论"] == "确认覆盖两条证据"
        assert closed["threshold_rule"]["threshold"] == ORG_THRESHOLD

    def test_single_occurrence_can_close_directly(self):
        batch = _uid("BATCH")
        dispatch = self._one_task(batch)
        single_task = next(t for t in dispatch["dispatched_tasks"]
                           if SINGLE_ORG in t["复核对象"])
        ok = AnnotReviewService().submit_candidate_review(
            single_task["复核编号"], "单出处证据确认", "复核员C")
        assert ok["status"] == "已结案"


class TestFullPipelineAndApi:
    """完整识别链路与 HTTP 路由契约。"""

    def test_recognize_pipeline_keeps_occurrences_and_triggers_relation(self):
        set_id = _write_chunks(_uid())
        result = EntityRecognizeService().recognize_from_chunk(set_id)
        assert result["entities"] >= 3
        rel_file = DATA_ROOT / "relations" / f"{set_id}_relations.jsonl"
        assert rel_file.exists(), "逐出处保留不应破坏关系抽取触发"

    def test_candidate_history_endpoints(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        build = review_router.build_candidates(
            BuildCandidatesPayload(chunk_set_id=set_id))
        run_id = build["run_id"]
        listed = review_router.list_candidates()
        assert listed["total"] == len(build["candidates"])
        history = review_router.candidate_run_history()
        assert any(item["run_id"] == run_id for item in history["items"])
        candidate_id = build["candidates"][0]["candidate_id"]
        detail = review_router.get_candidate(candidate_id)
        assert detail["candidate"]["candidate_id"] == candidate_id
        assert detail["history"], "候选详情需带各次 run 的阈值历史"

    def test_dispatch_and_conclusion_api_flow(self):
        set_id = _write_chunks(_uid())
        _recognize(set_id)
        review_router.build_candidates(
            BuildCandidatesPayload(chunk_set_id=set_id))
        batch = _uid("APIBATCH")
        dispatch = review_router.dispatch_candidate_todos(
            DispatchPayload(batch_key=batch))
        assert dispatch["tasks_created"] >= 2
        cross_task = next(t for t in dispatch["dispatched_tasks"]
                          if CROSS_ORG in t["复核对象"])
        # 跨阈值单独结案被路由层拒绝（HTTP 400）
        with pytest.raises(HTTPException) as exc:
            review_router.submit_candidate_conclusion(
                cross_task["复核编号"],
                CandidateConclusionPayload(
                    conclusion="只看正文", reviewer="接口复核员",
                    covered_evidence_ids=[cross_task["selected_sources"][0]["evidence_id"]]))
        assert exc.value.status_code == 400
        all_ids = [e["evidence_id"] for e in cross_task["all_evidence"]]
        ok = review_router.submit_candidate_conclusion(
            cross_task["复核编号"],
            CandidateConclusionPayload(
                conclusion="两条证据核对一致", reviewer="接口复核员",
                covered_evidence_ids=all_ids))
        assert ok["status"] == "已结案"
        closed = review_router.query_closed_reviews(
            candidate_id=cross_task["candidate_id"])
        assert closed["total"] == 1
        assert closed["items"][0]["threshold_rule"]["threshold"] == ORG_THRESHOLD
