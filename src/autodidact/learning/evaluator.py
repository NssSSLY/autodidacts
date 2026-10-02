# 文件职责：保留 Evaluator 名称作为 ClosedBookEvaluator 的兼容别名，不是第二套评估流程。
"""Compatibility entry point for the isolated closed-book protocol."""

from autodidact.learning.closed_book import ClosedBookEvaluator

Evaluator = ClosedBookEvaluator
