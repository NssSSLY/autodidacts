# 文件职责：定义考试、答案、核源评分与候选方法的结构化 schema。
from typing import Literal

from pydantic import BaseModel, Field


class ExamQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    rubric: str = Field(min_length=1, max_length=3000)
    kind: Literal["factual", "reasoning", "transfer"] = "factual"
    reference_answer: str = Field(min_length=1, max_length=3000)


class ExamPaper(BaseModel):
    questions: list[ExamQuestion] = Field(default_factory=list, max_length=8)


class ExamAnswer(BaseModel):
    answer: str = Field(max_length=5000)
    confidence: float = Field(ge=0, le=1)


class AnswerGrade(BaseModel):
    correct: bool
    factual_accuracy: float = Field(ge=0, le=1)
    reasoning: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)
    explanation: str = Field(max_length=3000)
    source_url: str = ""
    excerpt: str = Field(default="", max_length=2000)


class SkillDraft(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    description: str = Field(max_length=2000)
    trigger_condition: str = Field(min_length=1, max_length=1000)
    procedure: list[str] = Field(min_length=1, max_length=10)


class SkillDrafts(BaseModel):
    skills: list[SkillDraft] = Field(default_factory=list, max_length=3)
