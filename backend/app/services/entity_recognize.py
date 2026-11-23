"""实体识别服务：分块文本中人名、机构、时间、地名等命名实体的识别结果与置信度过滤。

从分块产物读取文本分块，执行命名实体识别并持久化实体结果，同时触发关系抽取。

本模块另外提供「实体复核候选集」能力：
- 按实体类别适用的置信度阈值，对命名实体数据逐出处（分块/标题或正文）判定；
- 记录每个样本采用的阈值规则、抽取出处（真实分块编号、章节、片段、偏移）；
- 同一实体名在多个分块出现时保留每条真实出处，跨阈值样本单独标记，
  供人工复核判断结论能否覆盖标题与正文两条证据；
- 每次构建生成不可变 run 快照，改阈值后可对比历史数据与旧结论；
- 证据指纹 + 规则键保证同一证据用同一规则重跑不重复派单。
"""
from __future__ import annotations

import time
import re
import json
import uuid
import hashlib
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService


class EntityRecognizeService(BaseModuleService):
    MODULE = "entity_recognize"
    ENTITY = "命名实体"
    OPERATION_TERM = "识别"
    LIST_FIELDS = ["实体编号", "分块编号", "实体类型", "实体文本", "起止偏移", "置信度", "模型类型", "模型版本"]
    REQUIRED_FIELDS = ["实体编号", "分块编号", "实体类型"]
    STATUS_ORDER = ["草稿", "待识别", "已识别", "已驳回"]
    ACTION_RULES = {"启动识别": "待识别", "确认识别": "已识别", "标记驳回": "已驳回"}
    NEGATIVE_ACTIONS = ["标记驳回"]

    # 按实体类别适用的默认置信度阈值；候选集判定与人工复核派单都以此为规则基线
    DEFAULT_TYPE_THRESHOLDS: dict[str, float] = {
        "PERSON": 0.90,
        "ORG": 0.85,
        "LOC": 0.85,
        "TIME": 0.90,
        "MONEY": 0.90,
        "TECH": 0.88,
    }
    FALLBACK_THRESHOLD = 0.85

    # 中文机构名按后缀回扫时的断词边界（连接词与常见动词/介词），
    # 纯中文连续串没有正则词边界，靠这些字确定机构名左边界
    ORG_SUFFIXES = ["有限公司", "集团", "公司"]
    ORG_STOP_CHARS = set("与和及、，。,. 的在对从把被让向为提到访见会谈联合跟同")

    ENTITY_PATTERNS = {
        "PERSON": [
            r"(张\w{1,3})", r"(李\w{1,3})", r"(王\w{1,3})",
            r"首席科学家",
        ],
        "ORG": [
            r"(\w+集团)", r"(\w+公司)", r"(\w+有限公司)",
            r"生态合作伙伴",
        ],
        "TIME": [
            r"(\d{4}年)", r"(\d+季度)", r"(\d+月\d+日)",
        ],
        "LOC": [
            r"(Beijing)", r"(Shanghai)", r"总部", r"分部",
        ],
        "MONEY": [
            r"(\d+亿\w*)", r"(\d+万\w*)",
        ],
        "TECH": [
            r"知识图谱", r"人工智能", r"信息抽取", r"文档解析",
        ],
    }

    def recognize_from_chunk(self, chunk_set_id: str) -> dict[str, Any]:
        """从分块产物读取文本并执行命名实体识别，将实体结果持久化到JSONL文件。"""
        # 读取分块产物作为输入源
        chunk_file = self._data_dir("chunks") / f"{chunk_set_id}_chunks.jsonl"
        chunks = self._read_jsonl(chunk_file)
        if not chunks:
            return {"error": f"分块产物 {chunk_set_id} 不存在或为空，请先执行分块切分"}

        model = "NER-BiLSTM-CRF"
        version = "v3.1.0"
        entities = []
        entity_id_counter = 0
# 数据操作：读取分块数据校验实体类型并抽取命名实体数据集

        for chunk in chunks:
            text = chunk.get("text", "")
            chunk_id = chunk.get("chunk_id", "")
            if not text:
                continue
            # 真实分块出处：章节（标题/正文）与来源分块编号，同名实体跨分块不合并
            section = self._detect_section(chunk)
            # 分块可携带逐实体的置信度提示（仿真/回归测试用），缺省走模型公式
            confidence_hints = chunk.get("entity_confidence") or {}

            # ORG 走确定性后缀回扫，其余类型走正则模式
            type_spans: list[tuple[str, int, int, str]] = [
                ("ORG", start, end, ent_text)
                for start, end, ent_text in self._extract_org_spans(text)
            ]
            for ent_type, patterns in self.ENTITY_PATTERNS.items():
                if ent_type == "ORG":
                    continue
                for pattern in patterns:
                    for match in re.finditer(pattern, text):
                        type_spans.append((ent_type, match.start(), match.end(), match.group(0)))

            for ent_type, start_offset, end_offset, ent_text in type_spans:
                entity_id_counter += 1

                # 计算置信度：优先采用分块给出的出处级提示，保证跨阈值场景可复现
                if ent_text in confidence_hints:
                    confidence = float(confidence_hints[ent_text])
                else:
                    confidence = self._model_confidence(ent_type, ent_text)

                entities.append({
                    "entity_id": f"ENT-{entity_id_counter:06d}",
                    "chunk_id": chunk_id,
# 数据产物：命名实体数据集写入entities目录持久化文件，触发下游关系数据集
                    "entity_type": ent_type,
                    "entity_text": ent_text,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                    "confidence": confidence,
                    "model": model,
                    "model_version": version,
                    "section": section,
                    "source_file": chunk_file.name,
                    "source_excerpt": self._excerpt(text, start_offset, end_offset),
                    "recognized_at": time.time(),
                })

        # 同一分块同一 span 折叠为一条；不同分块（标题/正文）的同名实体各自保留
        entities = self._collapse_occurrences(entities)

        output_path = self._data_dir("entities") / f"{chunk_set_id}_entities.jsonl"
        written = self._overwrite_jsonl(output_path, entities)

        # 写入检查点
        self._write_checkpoint(f"entity_{chunk_set_id}", {
            "chunk_set_id": chunk_set_id,
            "model": model,
            "chunks_processed": len(chunks),
            "entities_found": len(entities),
            "entity_types": list(set(e["entity_type"] for e in entities)),
            "avg_confidence": sum(e["confidence"] for e in entities) / max(len(entities), 1),
        })

        # 跨模块触发：关系抽取
        triggers = {}
        if entities and chunk_set_id:
            trig_rel = self._trigger_relation_extract(chunk_set_id)
# 数据产物：筛选排序实体数据集按类型过滤定位高置信度实体
            triggers["relation_extract"] = trig_rel

        return {
            "chunk_set_id": chunk_set_id,
            "chunks": len(chunks),
            "entities": len(entities),
            "records_written": written,
            "output": str(output_path),
            "triggers": triggers,
        }

    def _model_confidence(self, ent_type: str, ent_text: str) -> float:
        """NER 模型对一次命中的置信度估算（与历史版本公式保持一致）。"""
        confidence = 0.85
        if ent_type in ("PERSON", "ORG"):
            confidence = 0.88 + (len(ent_text) % 3) * 0.04
        elif ent_type in ("TIME", "MONEY"):
            confidence = 0.92
        elif ent_type in ("TECH"):
            confidence = 0.90
        elif ent_type == "LOC":
            confidence = 0.82
        return min(confidence, 0.99)

    def _extract_org_spans(self, text: str) -> list[tuple[int, int, str]]:
        """确定性抽取中文机构名：按后缀（有限公司/集团/公司）定位后回扫到断词边界。

        纯中文连续串没有正则词边界，贪婪正则会把「提到」「与」等前文吞进实体名；
        这里显式维护已占用区间，保证同一字符不会被多个后缀重复命中。
        """
        occupied = [False] * len(text)
        spans: list[tuple[int, int, str]] = []
        for suffix in self.ORG_SUFFIXES:
            for match in re.finditer(re.escape(suffix), text):
                start, end = match.start(), match.end()
                cursor = start
                while cursor > 0 and text[cursor - 1] not in self.ORG_STOP_CHARS \
                        and not occupied[cursor - 1]:
                    cursor -= 1
                if any(occupied[cursor:end]):
                    continue
                for k in range(cursor, end):
                    occupied[k] = True
                ent_text = text[cursor:end].strip()
                if len(ent_text) >= len(suffix) + 1:
                    spans.append((cursor, end, ent_text))
        # 非后缀机构词（生态合作伙伴等）仍由正则模式兜底
        for pattern in self.ENTITY_PATTERNS["ORG"]:
            for match in re.finditer(pattern, text):
                start, end = match.start(), match.end()
                if not any(occupied[start:end]) and match.group(0) not in {s[2] for s in spans}:
                    spans.append((start, end, match.group(0)))
        return sorted(spans, key=lambda item: item[0])

    @staticmethod
    def _detect_section(chunk: dict[str, Any]) -> str:
        """判定分块章节出处：显式 section 优先，否则首个分块视为标题、其余为正文。"""
        section = str(chunk.get("section") or "").strip()
        if section:
            return section
        if chunk.get("seq") in (1, "1"):
            return "标题"
        return "正文"

    @staticmethod
    def _excerpt(text: str, start: int, end: int, window: int = 12) -> str:
        """截取命中实体前后窗口内的原文片段，作为复核候选的抽取出处凭据。"""
        left = max(0, start - window)
        right = min(len(text), end + window)
        snippet = text[left:right].replace("\n", " ").strip()
        return snippet

    def _collapse_occurrences(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同一分块同一文本同一偏移只保留一条；不同分块的同名实体视为不同出处全部保留。"""
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        for ent in entities:
# 数据操作：折叠同span重复命中，保留实体在每个真实分块的出处
            key = (
                ent.get("chunk_id"), ent.get("entity_type"), ent.get("entity_text"),
                ent.get("start_offset"), ent.get("end_offset"),
            )
            if key not in seen:
                seen[key] = ent
        return sorted(seen.values(), key=lambda e: (str(e.get("chunk_id")), e["entity_id"]))

    def _deduplicate_entities(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按实体文本去重，保留置信度最高的记录（历史质检口径，跨分块分析请用候选集）。"""
        seen = {}
        for ent in entities:
# 数据操作：去重实体数据保留置信度最高的数据集记录
            key = (ent["entity_type"], ent["entity_text"])
            if key not in seen or ent["confidence"] > seen[key]["confidence"]:
                seen[key] = ent
        return sorted(seen.values(), key=lambda e: e["entity_id"])

    def filter_by_confidence(self, entities: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
        """按置信度阈值过滤实体识别结果。"""
        return [e for e in entities if e.get("confidence", 0) >= threshold]

    # -- 实体复核候选集 --

    @staticmethod
    def _stable_hash(prefix: str, *parts: Any) -> str:
        """基于业务键生成稳定短哈希，保证同一规则重跑得到同一候选/证据编号。"""
        raw = "|".join(str(p) for p in parts)
        return f"{prefix}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12].upper()}"

    def _resolve_thresholds(self, overrides: dict[str, float] | None) -> dict[str, float]:
        """合并类别阈值：调用方覆盖优先，未覆盖类别沿用默认阈值。"""
        merged = dict(self.DEFAULT_TYPE_THRESHOLDS)
        for ent_type, value in (overrides or {}).items():
            merged[ent_type] = float(value)
        return merged

    def _threshold_for(
        self, ent_type: str, thresholds: dict[str, float], override_types: set[str],
    ) -> tuple[float, str]:
        """返回某类别适用阈值与规则来源（type_default / override / fallback）。"""
        if ent_type in thresholds:
            source = "override" if ent_type in override_types else "type_default"
            return thresholds[ent_type], source
        return self.FALLBACK_THRESHOLD, "fallback"

    def build_review_candidates(
        self,
        chunk_set_id: str | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """从命名实体数据构建按类别置信度阈值校验的复核候选集。

        判定口径：同一 (实体类型, 实体文本) 聚为一个候选，其每条分块出处独立
        与适用阈值比较；只要存在低于阈值的出处即进入候选集。出处同时分布在
        阈值两侧时标记 cross_threshold，且不可单独结案，必须由复核员确认结论
        能否覆盖标题与正文两条证据。
        """
        applied = self._resolve_thresholds(thresholds)
        override_types = set((thresholds or {}).keys())

        if chunk_set_id:
            entity_files = [self._data_dir("entities") / f"{chunk_set_id}_entities.jsonl"]
            entity_files = [f for f in entity_files if f.exists()]
        else:
            entity_files = sorted(self._data_dir("entities").glob("*.jsonl"))
        if not entity_files:
            return {"error": "命名实体数据不存在，请先执行实体识别"}

        # (数据来源作用域, entity_type, entity_text) -> 聚合桶
        # 作用域取实体文件名（即分块集），不同文档的同名实体不共享候选与派单幂等键
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        total_evidence = 0
        for ef in entity_files:
            scope = ef.name.replace("_entities.jsonl", "")
            for ent in self._read_jsonl(ef):
                ent_type = str(ent.get("entity_type", ""))
                ent_text = str(ent.get("entity_text", ""))
                confidence = float(ent.get("confidence", 0.0))
                threshold, rule_source = self._threshold_for(ent_type, applied, override_types)
                key = (scope, ent_type, ent_text)
                bucket = buckets.setdefault(key, {"evidence": [], "threshold": threshold,
                                                  "rule_source": rule_source,
                                                  "source_scopes": []})
                if scope not in bucket["source_scopes"]:
                    bucket["source_scopes"].append(scope)
                fingerprint = self._stable_hash(
                    "EVD",
                    ef.name, ent.get("chunk_id", ""), ent_type, ent_text,
                    ent.get("start_offset"), ent.get("end_offset"), round(confidence, 6),
                )
                bucket["evidence"].append({
                    "evidence_id": fingerprint,
                    "entity_id": ent.get("entity_id", ""),
                    "chunk_id": ent.get("chunk_id", ""),
                    "section": ent.get("section") or "正文",
                    "confidence": confidence,
                    "applied_threshold": threshold,
                    "threshold_side": "above" if confidence >= threshold else "below",
                    "source_file": ef.name,
                    "chunk_source_file": ent.get("source_file", ""),
                    "source_excerpt": ent.get("source_excerpt", ""),
                    "start_offset": ent.get("start_offset"),
                    "end_offset": ent.get("end_offset"),
                })
                total_evidence += 1

        candidates: list[dict[str, Any]] = []
        for (scope, ent_type, ent_text), bucket in sorted(buckets.items()):
            evidence = sorted(bucket["evidence"], key=lambda e: (e["section"], e["chunk_id"]))
            threshold = bucket["threshold"]
            below = [e for e in evidence if e["threshold_side"] == "below"]
            above = [e for e in evidence if e["threshold_side"] == "above"]
            # 全部出处都达标的实体无需复核，不进入候选集
            if not below:
                continue
            cross = bool(below and above)
            # 候选 ID 含数据来源作用域：同集重跑稳定可比对，跨文档同名实体互不串单
            candidate_id = self._stable_hash("ERC", scope, ent_type, ent_text)
            candidates.append({
                "candidate_id": candidate_id,
                "source_scope": scope,
                "entity_type": ent_type,
                "entity_text": ent_text,
                "applied_threshold": threshold,
                "threshold_rule": {
                    "rule_source": bucket["rule_source"],
                    "entity_type": ent_type,
                    "threshold": threshold,
                },
                "evidence_count": len(evidence),
                "below_count": len(below),
                "above_count": len(above),
                "cross_threshold": cross,
                # 跨阈值样本仍关联着高分证据，结论无法只覆盖低分出处，不允许单独结案
                "independently_closable": not cross,
                "min_confidence": min(e["confidence"] for e in evidence),
                "max_confidence": max(e["confidence"] for e in evidence),
                "sections": sorted({e["section"] for e in evidence}),
                "evidence": evidence,
                "review_status": "待复核",
            })

        run_id = self._stable_hash(
            "RUN",
            int(time.time() * 1000),
            uuid.uuid4().hex,
            json.dumps(applied, sort_keys=True, ensure_ascii=False),
            ",".join(f.name for f in entity_files),
        )
        run_dir = self._data_dir("entity_review") / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = run_dir / f"{run_id}.jsonl"
        self._overwrite_jsonl(snapshot_path, candidates)

        manifest_record = {
            "run_id": run_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "thresholds": applied,
            "source_files": [f.name for f in entity_files],
            "candidate_count": len(candidates),
            "evidence_count": total_evidence,
            "cross_threshold_count": sum(1 for c in candidates if c["cross_threshold"]),
            "snapshot_file": str(snapshot_path),
        }
        self._write_jsonl(self._data_dir("entity_review") / "run_manifest.jsonl", [manifest_record])
        self._overwrite_jsonl(self._data_dir("entity_review") / "candidates_latest.jsonl", candidates)

        return {
            **manifest_record,
            "candidates": candidates,
            "latest_output": str(self._data_dir("entity_review") / "candidates_latest.jsonl"),
        }

    def list_review_candidates(
        self,
        run_id: str | None = None,
        entity_type: str | None = None,
        only_cross: bool = False,
    ) -> list[dict[str, Any]]:
        """读取候选集：默认读最新一次构建，指定 run_id 时读该次不可变快照。"""
        if run_id:
            path = self._data_dir("entity_review") / "runs" / f"{run_id}.jsonl"
        else:
            path = self._data_dir("entity_review") / "candidates_latest.jsonl"
        candidates = self._read_jsonl(path)
        if entity_type:
            candidates = [c for c in candidates if c.get("entity_type") == entity_type]
        if only_cross:
            candidates = [c for c in candidates if c.get("cross_threshold")]
        return candidates

    def get_review_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """查询单个候选，并汇总各次 run 的阈值历史与已登记的复核结论依据。"""
        latest = self._data_dir("entity_review") / "candidates_latest.jsonl"
        current = next((c for c in self._read_jsonl(latest)
                        if c.get("candidate_id") == candidate_id), None)
        history: list[dict[str, Any]] = []
        for manifest in self._read_jsonl(self._data_dir("entity_review") / "run_manifest.jsonl"):
            snapshot = self._read_jsonl(Path(manifest["snapshot_file"]))
            record = next((c for c in snapshot if c.get("candidate_id") == candidate_id), None)
            if record is None:
                continue
            history.append({
                "run_id": manifest["run_id"],
                "created_at": manifest["created_at"],
                "applied_threshold": record["applied_threshold"],
                "threshold_rule": record["threshold_rule"],
                "cross_threshold": record["cross_threshold"],
                "review_status": record.get("review_status"),
                "evidence_count": record["evidence_count"],
            })
        if current is None and not history:
            return None
        return {"candidate": current, "history": history}

    def list_candidate_runs(self) -> list[dict[str, Any]]:
        """返回历次候选集构建记录，改阈值后可据此对比历史数据与旧结论。"""
        return self._read_jsonl(self._data_dir("entity_review") / "run_manifest.jsonl")

    def _overwrite_jsonl(self, filepath: Path, records: list[dict[str, Any]]) -> int:
        """整文件覆盖写入 JSONL（候选集最新视图与 run 快照使用），返回写入条数。"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return len(records)
