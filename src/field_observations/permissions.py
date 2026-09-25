"""角色与权限常量。"""

ROLE_PERMISSIONS = {
    "reporter": {"observation.submit", "identification.add"},
    "curator": {"identification.add", "candidate.evaluate", "merge.decide"},
    "auditor": set(),
}

# 可以查看全部上报、候选与统一观察记录的角色。
READER_ROLES = {"curator", "auditor"}

# 位置与可见范围从严到宽排序。
VISIBILITY_LEVELS = {"public": 0, "restricted": 1, "sensitive": 2}

DECISION_ACTIONS = {"merge", "reject", "split"}

CANDIDATE_STATUSES = {"open", "merged", "rejected"}
