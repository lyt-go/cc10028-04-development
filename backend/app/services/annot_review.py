"""人工复核服务：低置信抽取结果的人工复核任务、复核结论登记与退回原因追溯。

从抽取质量报告筛选需要人工复核的低置信度条目，创建复核任务并记录结论。

复核候选集对接：
- 人工复核使用「实体复核候选集」创建待办，派单请求返回时保留每个样本采用的
  阈值规则和被选中的来源（真实分块、章节、片段）；
- 证据指纹 + 规则键构成派单幂等键，同一证据用同一规则重跑不重复派单；
- 跨阈值候选（标题/正文置信度分列阈值两侧）不可单独结案，结论必须能覆盖
  全部证据后才能登记；
- 已结案结果不可变，改阈值重跑候选集后仍可按原 run/规则/来源查询原结论。
"""
from __future__ import annotations

import time
import json
import hashlib
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService


class AnnotReviewService(BaseModuleService):
    MODULE = "annot_review"
    ENTITY = "复核任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["复核编号", "分块编号", "复核对象", "原判定", "复核结论", "复核人", "复核时间", "退回原因"]
    REQUIRED_FIELDS = ["复核编号", "分块编号", "复核对象"]
    STATUS_ORDER = ["草稿", "待复核", "复核中", "已结案"]
    ACTION_RULES = {"分派复核": "待复核", "提交结论": "已结案", "标记退回": "已结案"}
    NEGATIVE_ACTIONS = []

    def _review_dir(self) -> Path:
        return self._data_dir("entity_review")

    def _dispatch_ledger(self) -> Path:
        """派单台账：记录每条证据在某条规则下的派单状态，支撑幂等与历史查询。"""
        return self._review_dir() / "dispatch_ledger.jsonl"

    def _candidate_tasks_file(self, batch_key: str) -> Path:
        return self._review_dir() / f"{batch_key}_candidate_reviews.jsonl"

    def _all_review_files(self) -> list[Path]:
        files = sorted(self._data_dir("reviews").glob("*.jsonl"))
        files += sorted(self._review_dir().glob("*_candidate_reviews.jsonl"))
        return files

    def _read_ledger(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self._dispatch_ledger())

    def _append_ledger(self, entries: list[dict[str, Any]]) -> None:
        self._write_jsonl(self._dispatch_ledger(), entries)

    def create_review_tasks(self, batch_key: str, threshold: float = 0.80) -> dict[str, Any]:
        """从低置信度抽取结果中创建人工复核任务（历史口径，按统一阈值）。"""
        entity_files = sorted(self._data_dir("entities").glob("*.jsonl"))
        relation_files = sorted(self._data_dir("relations").glob("*.jsonl"))

        tasks = []
# 数据操作：校验复核任务数据创建低置信度条目的复核任务
        task_counter = 0

        # 筛选低置信度实体
        for ef in entity_files:
            entities = self._read_jsonl(ef)
            for e in entities:
                if e.get("confidence", 1.0) < threshold:
                    task_counter += 1
                    tasks.append({
                        "复核编号": f"REV-{task_counter:06d}",
                        "分块编号": e.get("chunk_id", ""),
                        "复核对象": f"实体: {e.get('entity_text')} [{e.get('entity_type')}]",
                        "原判定": f"置信度 {e.get('confidence', 0):.2f}",
                        "复核结论": "",
                        "复核人": "",
                        "复核时间": "",
                        "退回原因": "",
                        "status": "待复核",
                    })

        # 筛选低置信度关系
        for rf in relation_files:
            relations = self._read_jsonl(rf)
            for r in relations:
                if r.get("confidence", 1.0) < threshold:
                    task_counter += 1
                    tasks.append({
                        "复核编号": f"REV-{task_counter:06d}",
                        "分块编号": "",
                        "复核对象": f"关系: {r.get('head_entity')}→{r.get('relation_type')}→{r.get('tail_entity')}",
# 数据产物：复核任务清单写入reviews目录持久化文件
                        "原判定": f"置信度 {r.get('confidence', 0):.2f}",
                        "复核结论": "",
                        "复核人": "",
                        "复核时间": "",
                        "退回原因": "",
                        "status": "待复核",
                    })

        # 写入复核任务清单
        output_path = self._data_dir("reviews") / f"{batch_key}_reviews.jsonl"
        written = self._write_jsonl(output_path, tasks)

        self._write_checkpoint(f"review_{batch_key}", {
            "batch_key": batch_key,
            "threshold": threshold,
            "tasks_created": len(tasks),
            "entities_reviewed": sum(1 for t in tasks if "实体" in t["复核对象"]),
            "relations_reviewed": sum(1 for t in tasks if "关系" in t["复核对象"]),
        })

        return {
            "batch_key": batch_key,
            "threshold": threshold,
            "tasks_created": len(tasks),
            "records_written": written,
            "output": str(output_path),
        }

    # -- 复核候选集派单 --

    def create_review_todos_from_candidates(
        self,
        batch_key: str,
        candidate_ids: list[str] | None = None,
        run_id: str | None = None,
        reviewer: str = "",
    ) -> dict[str, Any]:
        """使用实体复核候选集创建人工复核待办。

        派单时为每个样本保留采用的阈值规则与被选中的来源；同一证据在同一
        规则下已派单则跳过，保证重跑不重复派单。
        """
        from app.services.entity_recognize import EntityRecognizeService

        entity_svc = EntityRecognizeService()
        candidates = entity_svc.list_review_candidates(run_id=run_id)
        if not candidates:
            return {"error": "实体复核候选集为空，请先按类别阈值构建候选集"}
        if candidate_ids:
            wanted = set(candidate_ids)
            candidates = [c for c in candidates if c.get("candidate_id") in wanted]
            missing = wanted - {c.get("candidate_id") for c in candidates}
            if missing:
                return {"error": f"候选不存在：{'、'.join(sorted(missing))}"}

        run_manifest = self._find_run_manifest(run_id)
        ledger = self._read_ledger()
        # 已派单幂等键：证据指纹 + 规则键（类别 + 阈值 + 规则来源）
        dispatched = {
            (item["evidence_id"], item["rule_key"])
            for item in ledger
        }
        # 全局已用复核编号，避免跨批次文件各自计数导致撞号、结案串单
        used_review_ids = {
            t.get("复核编号")
            for rf in self._all_review_files()
            for t in self._read_jsonl(rf)
        }

        tasks: list[dict[str, Any]] = []
        ledger_entries: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for candidate in candidates:
            rule = candidate["threshold_rule"]
            rule_key = f"{rule['entity_type']}@{rule['threshold']}:{rule['rule_source']}"
            selected_sources: list[dict[str, Any]] = []
            new_evidence: list[dict[str, Any]] = []
            for evidence in candidate["evidence"]:
                if evidence["threshold_side"] != "below":
                    # 跨阈值样本的高分证据也要带给复核员，但不单独派单
                    continue
                ev_id = evidence["evidence_id"]
                if (ev_id, rule_key) in dispatched:
                    skipped.append({"candidate_id": candidate["candidate_id"], "evidence_id": ev_id})
                    continue
                selected_sources.append({
                    "evidence_id": ev_id,
                    "entity_id": evidence.get("entity_id", ""),
                    "chunk_id": evidence.get("chunk_id", ""),
                    "section": evidence.get("section", ""),
                    "confidence": evidence.get("confidence"),
                    "source_file": evidence.get("source_file", ""),
                    "source_excerpt": evidence.get("source_excerpt", ""),
                    "start_offset": evidence.get("start_offset"),
                    "end_offset": evidence.get("end_offset"),
                })
                new_evidence.append(evidence)
            if not new_evidence and not selected_sources:
                # 该候选全部低分证据均已在同一规则下派过单
                continue

            # 全局稳定编号：候选+规则+批次决定，跨批次不撞号，重跑同一派单结果可复现
            review_id = "REV-C" + hashlib.sha1(
                f"{candidate['candidate_id']}|{rule_key}|{batch_key}".encode("utf-8")
            ).hexdigest()[:10].upper()
            if review_id in used_review_ids:
                # 极端碰撞时追加序号，保证编号唯一
                suffix = 2
                while f"{review_id}-{suffix}" in used_review_ids:
                    suffix += 1
                review_id = f"{review_id}-{suffix}"
            used_review_ids.add(review_id)
            task = {
                "复核编号": review_id,
                "分块编号": "、".join(sorted({e["chunk_id"] for e in selected_sources if e["chunk_id"]})),
                "复核对象": f"实体候选: {candidate['entity_text']} [{candidate['entity_type']}]",
                "原判定": (
                    f"置信度 {candidate['min_confidence']:.2f}~{candidate['max_confidence']:.2f}"
                    f"，适用阈值 {candidate['applied_threshold']:.2f}"
                ),
                "复核结论": "",
                "复核人": reviewer,
                "复核时间": "",
                "退回原因": "",
                "status": "待复核",
                # 候选派单上下文：规则、来源、结案约束全部随任务保留
                "candidate_id": candidate["candidate_id"],
                "run_id": run_id or (run_manifest or {}).get("run_id", ""),
                "threshold_rule": rule,
                "rule_key": rule_key,
                "cross_threshold": candidate["cross_threshold"],
                "independently_closable": candidate["independently_closable"],
                "sections": candidate.get("sections", []),
                "selected_sources": selected_sources,
                "all_evidence": candidate["evidence"],
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            tasks.append(task)
            for evidence in new_evidence:
                ledger_entries.append({
                    "evidence_id": evidence["evidence_id"],
                    "rule_key": rule_key,
                    "candidate_id": candidate["candidate_id"],
                    "review_id": review_id,
                    "batch_key": batch_key,
                    "chunk_id": evidence["chunk_id"],
                    "section": evidence["section"],
                    "dispatched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                dispatched.add((evidence["evidence_id"], rule_key))

        output_path = self._candidate_tasks_file(batch_key)
        written = self._write_jsonl(output_path, tasks)
        self._append_ledger(ledger_entries)

        return {
            "batch_key": batch_key,
            "run_id": run_id or (run_manifest or {}).get("run_id", ""),
            "tasks_created": len(tasks),
            "records_written": written,
            "skipped_dispatched": skipped,
            "dispatched_tasks": tasks,
            "output": str(output_path),
        }

    def _find_run_manifest(self, run_id: str | None) -> dict[str, Any] | None:
        from app.services.entity_recognize import EntityRecognizeService

        manifests = EntityRecognizeService().list_candidate_runs()
        if run_id:
            return next((m for m in manifests if m.get("run_id") == run_id), None)
        return manifests[-1] if manifests else None

    def submit_candidate_review(
        self,
        review_id: str,
        conclusion: str,
        reviewer: str,
        covered_evidence_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """登记候选复核结论；跨阈值候选必须声明结论覆盖全部证据才允许结案。"""
        review_files = self._all_review_files()
        for rf in review_files:
            tasks = self._read_jsonl(rf)
            for task in tasks:
                if task.get("复核编号") != review_id:
                    continue
                if task.get("status") == "已结案":
                    return {"error": f"复核任务 {review_id} 已结案，结论不可变更，请按原依据查询"}
                covered = set(covered_evidence_ids or [])
                all_ids = {e.get("evidence_id") for e in task.get("all_evidence", [])}
                if task.get("cross_threshold") and not all_ids.issubset(covered):
                    missing = sorted(all_ids - covered)
                    return {
                        "error": "该候选标题与正文证据分列阈值两侧，不能单独结案；"
                                 f"结论需覆盖全部证据，未覆盖：{'、'.join(missing)}",
                        "missing_evidence": missing,
                    }
                task["复核结论"] = conclusion
                task["复核人"] = reviewer
                task["复核时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["status"] = "已结案"
                task["closed_evidence_ids"] = sorted(covered or all_ids)
                self._overwrite_file(rf, tasks)
                return {
                    "review_id": review_id,
                    "candidate_id": task.get("candidate_id"),
                    "conclusion": conclusion,
                    "reviewer": reviewer,
                    "status": "已结案",
                    "threshold_rule": task.get("threshold_rule"),
                    "covered_evidence": task["closed_evidence_ids"],
                }
        return {"error": f"复核任务 {review_id} 不存在"}

    def query_closed_reviews(
        self,
        candidate_id: str | None = None,
        run_id: str | None = None,
        chunk_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按原依据查询已结案结果：候选、构建 run、真实分块来源均可作为查询键。"""
        closed: list[dict[str, Any]] = []
        for rf in self._all_review_files():
            for task in self._read_jsonl(rf):
                if task.get("status") != "已结案":
                    continue
                if candidate_id and task.get("candidate_id") != candidate_id:
                    continue
                if run_id and task.get("run_id") != run_id:
                    continue
                if chunk_id:
                    source_chunks = {s.get("chunk_id") for s in task.get("selected_sources", [])}
                    all_chunks = {e.get("chunk_id") for e in task.get("all_evidence", [])}
                    if chunk_id not in source_chunks and chunk_id not in all_chunks:
                        continue
                closed.append(task)
        return closed

    def submit_review_conclusion(self, review_id: str, conclusion: str, reviewer: str) -> dict[str, Any]:
        """提交复核结论并更新任务状态（历史统一阈值任务口径）。"""
        review_files = sorted(self._data_dir("reviews").glob("*.jsonl"))
        for rf in review_files:
            tasks = self._read_jsonl(rf)
            for task in tasks:
                if task.get("复核编号") == review_id:
                    task["复核结论"] = conclusion
                    task["复核人"] = reviewer
                    task["复核时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    task["status"] = "已结案"
                    self._overwrite_file(rf, tasks)
                    return {"review_id": review_id, "conclusion": conclusion,
                            "reviewer": reviewer, "status": "已结案"}
        return {"error": f"复核任务 {review_id} 不存在"}

    def _overwrite_file(self, filepath: Path, records: list[dict[str, Any]]) -> None:
        """整文件回写复核任务，保持文件内每条任务最新状态。"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
