from dataclasses import dataclass, field

# 固定字段压缩
@dataclass
class ContextCheckpoint:
    """Structured checkpoint produced by Layer 4 semantic compaction.

    A checkpoint is a Task Checkpoint (not a free-form conversation summary):
    it records facts that happened, user constraints, and remaining work.
    """
    active_request: str = ""  # 当前用户请求
    current_goal: str = ""   # 当前任务目标
    constraints: list[str] = field(default_factory=list)  # 用户约束
    files_read: list[str] = field(default_factory=list)  # 读取的文件
    files_modified: list[str] = field(default_factory=list)  # 修改的文件
    decisions: list[str] = field(default_factory=list)  # 决策
    test_status: str = ""                              # 测试状态
    remaining_work: list[str] = field(default_factory=list)  # 剩余工作
    errors: list[str] = field(default_factory=list)          # 未解决的错误
    open_questions: list[str] = field(default_factory=list)  # 开放问题
    next_steps: list[str] = field(default_factory=list)      # 下一步计划
    transcript_ref: str = ""                                # 完整历史的归档引用

    @classmethod
    def from_json(cls, text: str, transcript_ref: str = "") -> "ContextCheckpoint":
        import json
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return cls(
                current_goal=text[:500] if text else "",
                transcript_ref=transcript_ref,
            )
        return cls(
            active_request=data.get("active_request", ""),
            current_goal=data.get("current_goal", ""),
            constraints=data.get("constraints", []),
            files_read=data.get("files_read", []),
            files_modified=data.get("files_modified", []),
            decisions=data.get("decisions", []),
            test_status=data.get("test_status", ""),
            remaining_work=data.get("remaining_work", []),
            errors=data.get("errors", []),
            open_questions=data.get("open_questions", []),
            next_steps=data.get("next_steps", []),
            transcript_ref=transcript_ref,
        )
