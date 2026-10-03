# 文件职责：定义版本化真值题集、人工核查出处、白名单可复算评分、分集隔离和统计，不调用模型或执行题内代码。
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from math import sqrt
from statistics import mean
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RELIABLE_PROTOCOL = "reliable_benchmark_v1"
SCORING_PROTOCOL = "exact_numeric_v1"
CLOSED_BOOK_SCORING = "weighted_50202010_v1"
CLOSED_BOOK_WEIGHTS = {
    "factual_accuracy": 0.5,
    "reasoning": 0.2,
    "transfer": 0.2,
    "completeness": 0.1,
}
HELD_OUT = ("test", "transfer", "retention")


# 功能：固定JSON规范化摘要，不含时间随机性，复用原冻结题集的摘要算法。
def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# 功能：规范空白/大小写/Unicode宽度作文字去重，不声称识别语义等价。
def text_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


# 功能：解析有限且有界的十进制数，拒绝NaN/Infinity/超大指数和表达式。
def number(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("数值必须是至多80字符的十进制文字")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("非法十进制数") from exc
    if (
        not result.is_finite()
        or abs(result.as_tuple().exponent) > 100
        or abs(result.adjusted()) > 100
    ):
        raise ValueError("数值必须有限且指数不超过100")
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HumanReview(StrictModel):
    review_id: str = Field(min_length=1, max_length=100)
    reviewer: str = Field(min_length=1, max_length=100)
    role: Literal["human"]
    decision: Literal["approved"]
    reviewed_at: datetime
    note: str = Field(min_length=1, max_length=2000)

    # 功能：要求人工核查的显式时区与非未来时间；记录是用户声明，不认证专家身份。
    @model_validator(mode="after")
    def check_review(self):
        if self.reviewed_at.tzinfo is None or self.reviewed_at > datetime.now(UTC):
            raise ValueError("核查时间必须带时区且不能在未来")
        if not self.reviewer.strip() or not self.review_id.strip():
            raise ValueError("核查者与记录ID不能为空白")
        return self


class ComputedTruth(StrictModel):
    kind: Literal["computed"]
    operation: Literal["add", "subtract", "multiply", "divide"]
    operands: list[str] = Field(min_length=2, max_length=16)
    unit: str = Field(default="", max_length=30)
    absolute_tolerance: str = "0"

    # 功能：导入时校验计算可执行与非负容差，单位为精确文字，不猜测换算。
    @model_validator(mode="after")
    def check_computation(self):
        if not 0 <= number(self.absolute_tolerance) <= Decimal("0.01"):
            raise ValueError("绝对容差必须在0—0.01之间")
        computed_answer(self)
        return self


class ReviewedTruth(StrictModel):
    kind: Literal["reviewed"]
    accepted_answers: list[str] = Field(min_length=1, max_length=12)
    reference_uri: str = Field(min_length=1, max_length=1000)
    reference_text: str = Field(min_length=12, max_length=12000)
    reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review: HumanReview

    # 功能：核查快照摘要及非空答案，仅接受人工记录，不将模型rubric当真值。
    @model_validator(mode="after")
    def check_reference(self):
        if hashlib.sha256(self.reference_text.encode()).hexdigest() != self.reference_sha256:
            raise ValueError("真值参考快照摘要不匹配")
        if any(not text_key(a) or len(a) > 5000 for a in self.accepted_answers):
            raise ValueError("参考答案需非空且不超过5000字符")
        return self


class ReliableItem(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    family_id: str = Field(min_length=1, max_length=100)
    split: Literal["train", "test", "transfer", "retention"]
    category: str = Field(default="general", max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    truth: Annotated[ComputedTruth | ReviewedTruth, Field(discriminator="kind")]


class ReliableSuite(StrictModel):
    protocol: Literal["reliable_benchmark_v1"]
    suite_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    suite_version: int = Field(ge=1)
    scoring_protocol: Literal["exact_numeric_v1"] = SCORING_PROTOCOL
    min_retention_hours: int = Field(default=24, ge=1, le=8760)
    independence_review: HumanReview | None = None
    items: list[ReliableItem] = Field(min_length=3, max_length=200)

    # 功能：拒绝重复ID/规范题面/家族及训练评估污染，要求测试/迁移/保持三个独立分集。
    @model_validator(mode="after")
    def check_splits(self):
        for values in [
            [i.id for i in self.items],
            [i.family_id for i in self.items],
            [text_key(i.question) for i in self.items],
        ]:
            if any(not v.strip() for v in values) or len(set(values)) != len(values):
                raise ValueError("题ID、family_id和规范题面必须非空且独立唯一")
        if not set(HELD_OUT) <= {i.split for i in self.items}:
            raise ValueError("可靠题集必须包含test/transfer/retention，训练题不得代替评估题")
        return self


# 功能：仅用固定十进制上下文和四种白名单算术计算答案，绝不eval表达式或执行引用代码。
def computed_answer(truth):
    values = [number(v) for v in truth.operands]
    with localcontext() as context:
        context.prec = 50
        result = values[0]
        for operand in values[1:]:
            if truth.operation == "add":
                result += operand
            elif truth.operation == "subtract":
                result -= operand
            elif truth.operation == "multiply":
                result *= operand
            else:
                if operand == 0:
                    raise ValueError("计算真值不能除零")
                result /= operand
    if abs(result.adjusted()) > 100:
        raise ValueError("计算结果超出有界范围")
    return result


# 功能：按固定精确文字或十进制/精确单位规则重算二元得分，非格式答案判错而非调用裁判。
def grade_answer(item, answer):
    truth = item.truth
    if isinstance(truth, ReviewedTruth):
        correct = text_key(answer) in {text_key(a) for a in truth.accepted_answers}
        expected = truth.accepted_answers
    else:
        expected_value = computed_answer(truth)
        pattern = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
        if truth.unit:
            pattern += r"\s+" + re.escape(truth.unit)
        matched = re.fullmatch(pattern, answer.strip())
        try:
            with localcontext() as context:
                context.prec = 50
                correct = bool(
                    matched
                    and abs(number(matched.group(1)) - expected_value)
                    <= number(truth.absolute_tolerance)
                )
        except ValueError:
            correct = False
        expected = str(expected_value) + (" " + truth.unit if truth.unit else "")
    return {
        "score": float(correct),
        "correct": correct,
        "grading": SCORING_PROTOCOL,
        "truth_kind": truth.kind,
        "expected": expected,
        "rationale": "固定规则评分；计算可复算，人工记录仅为声明，不认证参考资料真实。",
    }


# 功能：汇总完整/未知数量、二元准确率、Brier校准和Wilson区间；失败不冒充错误或满分。
def summarize(details):
    scored = [d for d in details if d.get("status") == "scored"]
    n = len(scored)
    score = mean(d["score"] for d in scored) if n else None
    interval = None
    if n:
        z = 1.959963984540054
        den = 1 + z * z / n
        center = (score + z * z / (2 * n)) / den
        half = z * sqrt(score * (1 - score) / n + z * z / (4 * n * n)) / den
        interval = [max(0, center - half), min(1, center + half)]
    return {
        "n": len(details),
        "scored_n": n,
        "unknown_n": len(details) - n,
        "complete": n == len(details) and n > 0,
        "score": score,
        "wilson_95": interval,
        "calibration": 1 - mean(d["brier"] for d in scored) if n else None,
    }


# 功能：标识模型与服务端点摘要，不输出密钥或端点凭据，不保证远端模型版本未偷偷改变。
def model_identity(llm):
    inner = llm
    for _ in range(8):
        if not hasattr(inner, "inner"):
            break
        inner = inner.inner
    return {
        "provider": llm.provider_name,
        "model": llm.model_name,
        "endpoint_sha256": digest(getattr(inner, "base_url", "")),
        "sampling_sha256": digest(
            [getattr(inner, "default_temperature", None), getattr(inner, "max_output_tokens", None)]
        ),
    }


# 功能：给日常闭卷分数分配版本/公式标识，旧缺版本记录保留legacy而不猜测重算。
def evaluation_key(audit):
    return digest(
        {
            "protocol": audit.get("protocol", "legacy_unknown"),
            "scoring_protocol": audit.get("scoring_protocol", "legacy_unversioned"),
            "formula_sha256": audit.get("formula_sha256", "unknown"),
            "learner_provider": audit.get("learner_provider", "unknown"),
            "learner_model": audit.get("learner_model", "unknown"),
            "judge_model": audit.get("judge_model", "unknown"),
        }
    )


# 功能：把日常评估按协议/公式/模型分别汇总；旧缺版本保持legacy，绝不跨版本计算单个均值。
def evaluation_groups(evaluations):
    groups = {}
    for evaluation in evaluations:
        audit = (evaluation.components or {}).get("audit", {})
        key = evaluation_key(audit)
        group = groups.setdefault(
            key,
            {
                "scores": [],
                "protocol": audit.get("protocol", "legacy_unknown"),
                "scoring_protocol": audit.get("scoring_protocol", "legacy_unversioned"),
                "model": audit.get("learner_model", "unknown"),
            },
        )
        group["scores"].append(evaluation.score)
    return {
        key: {
            "n": len(group["scores"]),
            "mean_score": mean(group["scores"]),
            **{k: v for k, v in group.items() if k != "scores"},
        }
        for key, group in groups.items()
    }
