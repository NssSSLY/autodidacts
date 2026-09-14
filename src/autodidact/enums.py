from enum import StrEnum


class GoalStatus(StrEnum):
    DISCOVERED = "discovered"
    PLANNED = "planned"
    RESEARCHING = "researching"
    SYNTHESIZING = "synthesizing"
    TESTING = "testing"
    REFLECTING = "reflecting"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class GoalSource(StrEnum):
    HUMAN = "human"
    CURIOSITY = "curiosity"
    KNOWLEDGE_GAP = "knowledge_gap"
    CONFLICT = "conflict"
    FAILED_TEST = "failed_test"
    PREREQUISITE = "prerequisite"
    FOLLOW_UP = "follow_up"


class BeliefStatus(StrEnum):
    PROVISIONAL = "provisional"
    SUPPORTED = "supported"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    WEAKENED = "weakened"
    RETRACTED = "retracted"
    UNRESOLVED = "unresolved"


class DisputeStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED_OLD = "resolved_old"
    RESOLVED_NEW = "resolved_new"
    RESOLVED_CONDITIONAL = "resolved_conditional"
    UNRESOLVED = "unresolved"


class AccessType(StrEnum):
    API = "api"
    WEB = "web"
    LOCAL = "local"


class EvidenceStance(StrEnum):
    SUPPORT = "support"
    ATTACK = "attack"
    CONTEXT = "context"
