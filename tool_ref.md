# 一个 Agent Tool 抽象基类设计

核心思路是：**Tool 不是普通函数，而是带 schema、权限、错误语义、可观测性、执行策略的受控能力单元**。

Python 的 `abc` 模块适合用来定义这种强约束接口；Pydantic 可以从模型自动生成 JSON Schema；OpenAI Agents SDK 也强调 tool 的 name、description、input schema 与执行函数之间的绑定；MCP 规范同样以 JSON Schema 作为工具参数校验基础。([Python documentation][1]) LangChain 的 `BaseTool` 也采用类似方向：工具应有统一基类、参数 schema 与执行接口。([LangChain参考文档][2])

---

## 设计目标

一个好的 Agent Tool 基类应该同时解决这些问题：

1. **LLM 可发现**：有稳定的 `name`、清晰的 `description`、JSON Schema 参数说明。
2. **程序可验证**：输入必须经过强类型校验，不能把任意 dict 直接传进业务逻辑。
3. **执行可控**：支持 timeout、权限、确认、幂等性、side effect 标注。
4. **结果可解释**：返回结构化结果，而不是随意返回字符串。
5. **错误可恢复**：区分 validation error、permission error、timeout、transient error、execution error。
6. **框架可适配**：同一个 Tool 可以导出为 OpenAI function tool、MCP tool、LangChain tool 等。
7. **可观测**：每次调用都有 trace id、call id、耗时、metadata。

---

## 推荐基类实现

```python
from __future__ import annotations

import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar, Generic, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError


InputT = TypeVar("InputT", bound=BaseModel)


class ToolErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_ERROR = "transient_error"
    EXECUTION_ERROR = "execution_error"
    UNSUPPORTED = "unsupported"


class ToolPolicy(BaseModel):
    """
    描述工具的执行策略，供 Agent runtime / orchestrator 使用。

    side_effects:
        True 表示该工具会修改外部世界，例如写文件、发邮件、下单、删除资源。
    idempotent:
        True 表示重复执行不会产生额外副作用。
    requires_confirmation:
        True 表示执行前应要求用户或上层策略确认。
    permissions:
        工具需要的权限集合，例如 {"filesystem.read", "network.http"}。
    timeout_s:
        单次调用超时时间。None 表示不由工具基类控制。
    """

    side_effects: bool = False
    idempotent: bool = True
    requires_confirmation: bool = False
    permissions: set[str] = Field(default_factory=set)
    timeout_s: float | None = 30.0
    max_retries: int = 0


class ToolContext(BaseModel):
    """
    每次工具调用的运行上下文。

    注意：不要把 secrets、tokens、完整系统状态直接暴露给 LLM。
    如果确实需要，可由 runtime 通过私有对象或安全句柄注入。
    """

    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    call_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    user_id: str | None = None
    workspace_id: str | None = None

    # runtime 授予该次调用的权限
    granted_permissions: set[str] = Field(default_factory=set)

    # 上层 runtime 可以放入非敏感元数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class ToolArtifact(BaseModel):
    """
    工具产生的外部产物，例如文件、图片、报告、日志片段。
    """

    kind: str
    uri: str
    name: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    code: ToolErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """
    统一工具返回值。

    content:
        给 LLM 看的简短文本。
    data:
        给程序消费的结构化数据。
    artifacts:
        工具产生的文件、图片、表格等外部对象。
    metadata:
        trace、耗时、命中缓存、调用后统计等。
    """

    ok: bool
    content: str = ""
    data: Any | None = None
    artifacts: list[ToolArtifact] = Field(default_factory=list)
    error: ToolError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(
        cls,
        *,
        content: str = "",
        data: Any | None = None,
        artifacts: list[ToolArtifact] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=True,
            content=content,
            data=data,
            artifacts=artifacts or [],
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        *,
        code: ToolErrorCode,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            content=message,
            error=ToolError(
                code=code,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
            metadata=metadata or {},
        )


class ToolExecutionError(Exception):
    """
    业务代码主动抛出的工具错误。

    不建议让任意异常直接穿透给 Agent，因为异常中可能包含路径、密钥、
    SQL、栈信息或其他不应暴露的内容。
    """

    def __init__(
        self,
        message: str,
        *,
        code: ToolErrorCode = ToolErrorCode.EXECUTION_ERROR,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class ToolSpec(BaseModel):
    name: str
    description: str
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    policy: ToolPolicy
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseAgentTool(ABC, Generic[InputT]):
    """
    Agent Tool 抽象基类。

    子类必须声明：
    - name
    - description
    - args_schema
    - execute()

    可选声明：
    - output_schema
    - version
    - policy
    - tags
    """

    name: ClassVar[str]
    description: ClassVar[str]
    args_schema: ClassVar[type[InputT]]

    output_schema: ClassVar[type[BaseModel] | None] = None
    version: ClassVar[str] = "1.0.0"
    policy: ClassVar[ToolPolicy] = ToolPolicy()
    tags: ClassVar[list[str]] = []

    # 是否把未知异常的内部 details 暴露给结果。生产环境建议 False。
    expose_internal_errors: ClassVar[bool] = False

    @classmethod
    def spec(cls) -> ToolSpec:
        cls._validate_class_contract()

        return ToolSpec(
            name=cls.name,
            description=cls.description,
            version=cls.version,
            input_schema=cls.args_schema.model_json_schema(),
            output_schema=(
                cls.output_schema.model_json_schema()
                if cls.output_schema is not None
                else None
            ),
            policy=cls.policy,
            tags=list(cls.tags),
        )

    @classmethod
    def to_openai_chat_tool(cls) -> dict[str, Any]:
        """
        适配传统 Chat Completions / function calling 风格。

        不同 SDK 的 tool 格式可能略有变化，因此建议把 adapter
        放在边界层，而不是污染业务 Tool 实现。
        """

        s = cls.spec()
        return {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.input_schema,
            },
        }

    @classmethod
    def to_mcp_tool(cls) -> dict[str, Any]:
        """
        适配 MCP tool 描述。
        """

        s = cls.spec()
        return {
            "name": s.name,
            "description": s.description,
            "inputSchema": s.input_schema,
            "_meta": {
                "version": s.version,
                "tags": s.tags,
                "policy": s.policy.model_dump(),
            },
        }

    async def ainvoke(
        self,
        raw_args: Mapping[str, Any] | str | InputT,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """
        Agent runtime 应调用这个方法，而不是直接调用 execute()。

        它负责：
        - 参数解析与校验
        - 权限检查
        - confirmation 检查
        - timeout
        - 异常归一化
        - 结果归一化
        - metadata 注入
        """

        ctx = ctx or ToolContext()
        started_at = time.perf_counter()

        try:
            args = self.parse_args(raw_args)
        except ValidationError as e:
            return ToolResult.failure(
                code=ToolErrorCode.VALIDATION_ERROR,
                message=f"Invalid arguments for tool `{self.name}`.",
                retryable=False,
                details={"errors": e.errors()},
                metadata=self._base_metadata(ctx, started_at),
            )
        except Exception as e:
            return ToolResult.failure(
                code=ToolErrorCode.VALIDATION_ERROR,
                message=f"Failed to parse arguments for tool `{self.name}`.",
                retryable=False,
                details={"error_type": type(e).__name__},
                metadata=self._base_metadata(ctx, started_at),
            )

        try:
            self._check_policy(args, ctx)

            coro = self.execute(args, ctx)

            if self.policy.timeout_s is None:
                raw_result = await coro
            else:
                raw_result = await asyncio.wait_for(coro, timeout=self.policy.timeout_s)

            result = self._normalize_result(raw_result)
            result.metadata.update(self._base_metadata(ctx, started_at))
            return result

        except asyncio.TimeoutError:
            return ToolResult.failure(
                code=ToolErrorCode.TIMEOUT,
                message=f"Tool `{self.name}` timed out.",
                retryable=True,
                metadata=self._base_metadata(ctx, started_at),
            )

        except ToolExecutionError as e:
            return ToolResult.failure(
                code=e.code,
                message=str(e),
                retryable=e.retryable,
                details=e.details,
                metadata=self._base_metadata(ctx, started_at),
            )

        except Exception as e:
            details: dict[str, Any] = {"error_type": type(e).__name__}
            if self.expose_internal_errors:
                details["message"] = str(e)

            return ToolResult.failure(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=f"Tool `{self.name}` failed during execution.",
                retryable=False,
                details=details,
                metadata=self._base_metadata(ctx, started_at),
            )

    def invoke(
        self,
        raw_args: Mapping[str, Any] | str | InputT,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """
        同步调用入口。

        注意：如果当前线程已经在 event loop 里，应直接 await ainvoke()。
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ainvoke(raw_args, ctx))

        raise RuntimeError(
            "Cannot call invoke() inside a running event loop. "
            "Use `await tool.ainvoke(...)` instead."
        )

    def parse_args(self, raw_args: Mapping[str, Any] | str | InputT) -> InputT:
        if isinstance(raw_args, self.args_schema):
            return raw_args

        if isinstance(raw_args, str):
            return self.args_schema.model_validate_json(raw_args)

        if isinstance(raw_args, Mapping):
            return self.args_schema.model_validate(dict(raw_args))

        raise TypeError(
            f"Tool arguments must be {self.args_schema.__name__}, dict, or JSON string."
        )

    def _check_policy(self, args: InputT, ctx: ToolContext) -> None:
        missing_permissions = self.policy.permissions - ctx.granted_permissions
        if missing_permissions:
            raise ToolExecutionError(
                f"Permission denied for tool `{self.name}`.",
                code=ToolErrorCode.PERMISSION_DENIED,
                retryable=False,
                details={"missing_permissions": sorted(missing_permissions)},
            )

        if self.policy.requires_confirmation and not ctx.metadata.get("confirmed", False):
            raise ToolExecutionError(
                f"Confirmation required before executing tool `{self.name}`.",
                code=ToolErrorCode.CONFIRMATION_REQUIRED,
                retryable=False,
            )

    def _normalize_result(self, raw_result: Any) -> ToolResult:
        if isinstance(raw_result, ToolResult):
            return raw_result

        if isinstance(raw_result, BaseModel):
            return ToolResult.success(
                content=raw_result.model_dump_json(),
                data=raw_result.model_dump(),
            )

        if isinstance(raw_result, (dict, list)):
            return ToolResult.success(
                content=json.dumps(raw_result, ensure_ascii=False, default=str),
                data=raw_result,
            )

        if raw_result is None:
            return ToolResult.success(content="", data=None)

        return ToolResult.success(content=str(raw_result), data=raw_result)

    @classmethod
    def _validate_class_contract(cls) -> None:
        if not getattr(cls, "name", None):
            raise TypeError(f"{cls.__name__} must define non-empty `name`.")

        if not getattr(cls, "description", None):
            raise TypeError(f"{cls.__name__} must define non-empty `description`.")

        if not getattr(cls, "args_schema", None):
            raise TypeError(f"{cls.__name__} must define `args_schema`.")

        if not issubclass(cls.args_schema, BaseModel):
            raise TypeError("`args_schema` must be a Pydantic BaseModel subclass.")

        if cls.output_schema is not None and not issubclass(cls.output_schema, BaseModel):
            raise TypeError("`output_schema` must be a Pydantic BaseModel subclass.")

    def _base_metadata(
        self,
        ctx: ToolContext,
        started_at: float,
    ) -> dict[str, Any]:
        return {
            "tool_name": self.name,
            "tool_version": self.version,
            "trace_id": ctx.trace_id,
            "call_id": ctx.call_id,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
        }

    @abstractmethod
    async def execute(self, args: InputT, ctx: ToolContext) -> Any:
        """
        子类只实现核心业务逻辑。

        不应在这里重复做：
        - JSON 解析
        - 权限检查
        - timeout
        - 通用异常归一化
        """
        raise NotImplementedError
```

---

## 同步 Tool 的便捷子类

很多工具本质上是同步函数，例如本地计算、简单文件操作。可以提供一个同步基类，把同步逻辑安全地包进线程池。

```python
class SyncAgentTool(BaseAgentTool[InputT], ABC):
    async def execute(self, args: InputT, ctx: ToolContext) -> Any:
        return await asyncio.to_thread(self.run, args, ctx)

    @abstractmethod
    def run(self, args: InputT, ctx: ToolContext) -> Any:
        raise NotImplementedError
```

---

## 示例：一个无副作用 Tool

```python
class AddArgs(BaseModel):
    a: float = Field(description="First number.")
    b: float = Field(description="Second number.")


class AddOutput(BaseModel):
    result: float


class AddTool(BaseAgentTool[AddArgs]):
    name = "math_add"
    description = "Add two numbers and return the result."
    args_schema = AddArgs
    output_schema = AddOutput
    version = "1.0.0"
    policy = ToolPolicy(
        side_effects=False,
        idempotent=True,
        requires_confirmation=False,
        permissions=set(),
        timeout_s=2.0,
    )
    tags = ["math", "deterministic"]

    async def execute(self, args: AddArgs, ctx: ToolContext) -> AddOutput:
        return AddOutput(result=args.a + args.b)
```

调用：

```python
tool = AddTool()

result = await tool.ainvoke({"a": 1, "b": 2})

assert result.ok
print(result.content)
print(result.data)
```

导出为 OpenAI function tool：

```python
openai_tool_schema = AddTool.to_openai_chat_tool()
```

导出为 MCP tool：

```python
mcp_tool_schema = AddTool.to_mcp_tool()
```

---

## 示例：一个有副作用的文件写入 Tool

```python
from pathlib import Path


class WriteFileArgs(BaseModel):
    path: str = Field(description="Relative file path under the workspace.")
    content: str = Field(description="Text content to write.")
    overwrite: bool = Field(default=False, description="Whether to overwrite existing file.")


class WriteFileOutput(BaseModel):
    path: str
    bytes_written: int


class WriteFileTool(BaseAgentTool[WriteFileArgs]):
    name = "filesystem_write_file"
    description = "Write text content to a file inside the current workspace."
    args_schema = WriteFileArgs
    output_schema = WriteFileOutput
    version = "1.0.0"

    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=True,
        permissions={"filesystem.write"},
        timeout_s=5.0,
    )

    async def execute(self, args: WriteFileArgs, ctx: ToolContext) -> WriteFileOutput:
        if ctx.workspace_id is None:
            raise ToolExecutionError(
                "No workspace is available for file writing.",
                code=ToolErrorCode.PERMISSION_DENIED,
            )

        workspace = Path(ctx.workspace_id).resolve()
        target = (workspace / args.path).resolve()

        if not str(target).startswith(str(workspace)):
            raise ToolExecutionError(
                "Path escapes workspace.",
                code=ToolErrorCode.PERMISSION_DENIED,
            )

        if target.exists() and not args.overwrite:
            raise ToolExecutionError(
                "File already exists and overwrite is false.",
                code=ToolErrorCode.EXECUTION_ERROR,
                retryable=False,
                details={"path": str(target)},
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.content, encoding="utf-8")

        return WriteFileOutput(
            path=str(target.relative_to(workspace)),
            bytes_written=len(args.content.encode("utf-8")),
        )
```

调用时必须显式授予权限和确认：

```python
ctx = ToolContext(
    workspace_id="/tmp/agent-workspace",
    granted_permissions={"filesystem.write"},
    metadata={"confirmed": True},
)

result = await WriteFileTool().ainvoke(
    {
        "path": "notes/todo.md",
        "content": "# TODO\n",
        "overwrite": True,
    },
    ctx,
)
```

---

## 关键设计取舍

### 1. `args_schema` 必须是 Pydantic Model

不要让工具直接接收裸 `dict[str, Any]`。Agent 生成的参数经常缺字段、字段类型错误、枚举值错误，schema 校验应该发生在工具边界。

推荐：

```python
class SearchArgs(BaseModel):
    query: str = Field(min_length=1, description="Search query.")
    top_k: int = Field(default=5, ge=1, le=20)
```

不推荐：

```python
async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Any:
    ...
```

### 2. Tool 返回值应区分 `content` 与 `data`

`content` 是给 LLM 继续推理看的摘要，应该短、稳定、无敏感信息。

`data` 是给程序消费的结构化结果，可以更完整。

例如搜索工具可以返回：

```python
ToolResult.success(
    content="Found 5 matching documents.",
    data={
        "documents": [
            {"id": "doc_1", "title": "...", "score": 0.91},
        ]
    },
)
```

### 3. Side-effect Tool 必须显式标注

发邮件、写文件、删库、付款、部署、提交代码，这些都应该：

```python
policy = ToolPolicy(
    side_effects=True,
    idempotent=False,
    requires_confirmation=True,
    permissions={"email.send"},
)
```

然后由 Agent runtime 决定是否需要用户确认、权限提升、审计日志或 dry-run。

### 4. 错误不要直接暴露 Python 异常

不要让工具返回完整 traceback 给 LLM。更好的方式是：

```python
raise ToolExecutionError(
    "Database query failed.",
    code=ToolErrorCode.TRANSIENT_ERROR,
    retryable=True,
)
```

这样 Agent 可以知道：这不是参数错误，而是可以重试的瞬时错误。

### 5. Adapter 层和业务 Tool 分离

不要让业务 Tool 直接依赖 LangChain / OpenAI / MCP。更好的结构是：

```text
agent_tools/
  base.py
  builtin/
    filesystem.py
    shell.py
    search.py
  adapters/
    openai.py
    langchain.py
    mcp.py
    langgraph.py
```

`BaseAgentTool` 只定义稳定语义。具体框架格式通过 adapter 转换。

---

## 最小推荐目录结构

```text
agent_runtime/
  tools/
    base.py              # BaseAgentTool, ToolResult, ToolPolicy
    registry.py          # ToolRegistry
    adapters/
      openai.py
      langchain.py
      mcp.py
    builtin/
      file_read.py
      file_write.py
      shell.py
      web_search.py
```

---

## 可选：Tool Registry

如果你要让 Agent 动态发现、筛选、加载工具，可以加一个 registry。

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseAgentTool[Any]] = {}

    def register(self, tool: BaseAgentTool[Any]) -> None:
        spec = tool.spec()

        if spec.name in self._tools:
            raise ValueError(f"Tool `{spec.name}` is already registered.")

        self._tools[spec.name] = tool

    def get(self, name: str) -> BaseAgentTool[Any]:
        return self._tools[name]

    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]

    def search(self, *, tag: str | None = None) -> list[ToolSpec]:
        specs = self.list_specs()

        if tag is None:
            return specs

        return [s for s in specs if tag in s.tags]
```

---

## 最终建议

你的基类可以遵循这个核心接口：

```python
class BaseAgentTool(ABC, Generic[InputT]):
    name: ClassVar[str]
    description: ClassVar[str]
    args_schema: ClassVar[type[InputT]]
    output_schema: ClassVar[type[BaseModel] | None]
    policy: ClassVar[ToolPolicy]

    async def ainvoke(...) -> ToolResult:
        ...

    @abstractmethod
    async def execute(self, args: InputT, ctx: ToolContext) -> Any:
        ...
```

其中：

* `execute()` 是**业务逻辑层**；
* `ainvoke()` 是**受控调用层**；
* `ToolPolicy` 是**安全与调度层**；
* `ToolResult` 是**Agent 可理解的观测结果**；
* `to_openai_chat_tool()` / `to_mcp_tool()` 是**框架适配层**。

这个设计的优点是：后续无论你接 LangGraph、LangChain、OpenAI Agents SDK、MCP，还是自己写 Agent runtime，都不需要重写工具本体，只需要写 adapter。

[1]: https://docs.python.org/3/library/abc.html "abc — Abstract Base Classes — Python 3.14.5 documentation"
[2]: https://reference.langchain.com/python/langchain-core/tools/base/BaseTool?utm_source=chatgpt.com "BaseTool | langchain_core"
