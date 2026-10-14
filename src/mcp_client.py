# -*- coding: utf-8 -*-
"""
mcp_client.py —— 麦当劳 MCP 客户端（标准 Streamable HTTP / MCP 协议）

⚠️ 重要变更说明
    麦当劳 MCP（https://mcp.mcd.cn）使用的是**标准 MCP 协议（Streamable HTTP 传输）**，
    不是普通 REST。因此：
        ❌ 不要用 requests 去拼 GET /menu、/products/{id}/nutrition 这类 REST 路径
        ✅ 正确做法：用官方 mcp Python SDK 建立会话 →
                    tools/list 发现工具 → tools/call 调用工具

职责：
    1. 读取本地密钥配置（mcp-config.json / 环境变量）
    2. 用 mcp SDK 连接 MCP 服务，完成 initialize 握手（Authorization: Bearer {token}）
    3. tools/list  → 获取并打印麦当劳 MCP 提供的全部工具列表
    4. tools/call  → 分别获取 菜单列表 / 营养信息 / 过敏原信息 / 价格
    5. 统一异常捕获（含嵌套 ExceptionGroup 解包）、Token 脱敏、离线 mock 模式

安全：
    - 真实 Token 只从本地 mcp-config.json 或环境变量读取，绝不硬编码
    - 日志中 Token 自动脱敏（仅显示前 4 位）

依赖：pip install mcp
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("mcp_client")

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

# 默认配置文件路径：优先读取项目根目录下的 mcp-config.json
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp-config.json"
CONFIG_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "mcp-config.example.json"

# 营养字段可能的多种命名，做兼容处理（不同 MCP 工具返回的字段名可能不同）
NUTRITION_KEYS = {
    "calories": ("energyKcal", "calories", "kcal", "energy_kcal", "energy", "热量", "能量"),
    "protein": ("protein", "protein_g", "蛋白质"),
    "fat": ("fat", "fat_g", "脂肪"),
    "carbs": ("carbohydrate", "carbs", "carbohydrate_g", "碳水", "碳水化合物"),
    "sodium": ("sodium", "sodium_mg", "钠"),
}
ALLERGEN_KEYS = ("allergens", "allergen", "allergen_list", "allergenList", "过敏原")
PRICE_KEYS = ("price", "currentPrice", "unit_price", "unitPrice", "amount", "价格")

# 菜单列表可能出现的容器字段名
LIST_KEYS = ("data", "products", "items", "list", "menu", "result", "records", "rows")

# ---------------------------------------------------------------------------
# 真实麦当劳 MCP 的工具映射（依据 https://mcp.mcd.cn 实测 tools/list 结果确定）
#   query-meals          → 餐品列表（返回 code + name + currentPrice，一次拿到菜单与价格）
#   list-nutrition-foods → 营养成分表（无需参数，按 productName 索引）
#   calculate-price      → 价格试算（本 MVP 用 query-meals 的 currentPrice 即可）
#   ⚠️ 该 MCP 未提供「过敏原」工具（见 MCP_INTEGRATION.md 说明）
# ---------------------------------------------------------------------------
DEFAULT_TOOL_MAP: Dict[str, str] = {
    "menu": "query-meals",
    "nutrition": "list-nutrition-foods",
    "price": "query-meals",
    "allergen": "",   # 麦当劳 MCP 暂无过敏原工具
}

# ---------------------------------------------------------------------------
# 工具发现关键词（当 DEFAULT_TOOL_MAP 未命中时的自动匹配兜底）
# ---------------------------------------------------------------------------
TOOL_HINTS: Dict[str, tuple] = {
    "menu": ("query-meals", "meals", "menu", "product", "item", "category", "餐品", "菜单", "商品", "菜品"),
    "nutrition": ("list-nutrition-foods", "nutrition", "nutrient", "calorie", "energy", "营养", "热量", "成分"),
    "allergen": ("allergen", "allergy", "过敏", "致敏"),
    "price": ("calculate-price", "query-meals", "price", "cost", "amount", "价格", "定价", "售价"),
}

# ---------------------------------------------------------------------------
# 配料 → 过敏原 推断关键词（方案B：配料表推断）
# ---------------------------------------------------------------------------
# 背景：实测麦当劳 MCP 的 query-meal-detail 返回中包含「可特调配料」清单
#       （modification.items[].values[].name），例如：吉士 / 高达干酪片 /
#       醇香芝士风味酱 / 奶油球 / 黄油 / 麦香鱼酱 等。
# 用法：这些配料名可用于**推断疑似过敏原**。
# ⚠️ 三条重要约束：
#   1. 该清单是「可特调项」，**不是完整配料表**（如汉堡胚、面粉不会出现在里面），
#      因此推断结果必然不完整。
#   2. 因此本推断**只用于标记风险**（把疑似含过敏原的餐品排除 / 告警），
#      **绝不用于声明某餐品「不含过敏原」**。
#   3. 关键词来源为实测数据；此处为「食材名」片段匹配，与用户输入的
#      ALLERGEN_ALIAS（禁止单字 key）规则不同，故允许「鱼」这类单字片段。
INGREDIENT_ALLERGEN_KEYWORDS: Dict[str, tuple] = {
    "milk": ("吉士", "干酪", "芝士", "黄油", "奶油", "乳酪", "奶昔", "鲜奶", "牛奶"),
    "peanuts": ("花生",),
    "egg": ("沙拉酱", "蛋黄", "鸡蛋"),
    "gluten": ("面包", "汉堡胚", "面粉", "小麦", "麸质"),
    "soy": ("大豆", "黄豆", "酱油", "豆浆"),
    "fish": ("鱼",),
    "shellfish": ("虾", "蟹", "贝", "海鲜"),
    "nuts": ("坚果",),
    "sesame": ("芝麻",),
}


class McpError(Exception):
    """MCP 调用相关的自定义异常，方便上层统一捕获。"""


# ---------------------------------------------------------------------------
# 数据模型（保持与原实现一致，推荐引擎无需改动）
# ---------------------------------------------------------------------------

@dataclass
class Product:
    """统一的餐品数据结构（已由 MCP 工具返回结果归一化而来）。

    说明：
        allergens 为空集合时表示"无已知过敏原"；
        但若字段本身缺失（unknown），会把 allergen_known 置为 False，
        推荐引擎将采取安全优先策略直接过滤该餐品。
    """

    product_id: str                     # 餐品唯一 ID
    name: str = "未知餐品"               # 餐品名称
    category: str = "未分类"             # 分类（汉堡 / 饮料 / 小食 ...）
    calories: float = 0.0               # 热量（kcal）
    protein: float = 0.0                # 蛋白质（g）
    fat: float = 0.0                    # 脂肪（g）
    carbs: float = 0.0                  # 碳水化合物（g）
    sodium: float = 0.0                 # 钠（mg）
    price: float = 0.0                  # 价格（元）
    allergens: Set[str] = field(default_factory=set)  # 过敏原集合（小写）
    allergen_known: bool = True         # 过敏原数据是否可用（False = 缺失 / 非权威）
    # 过敏原数据来源，便于统计与排查：
    #   "mcp"                 —— MCP 提供的权威过敏原数据
    #   "ingredient_inferred" —— 由 query-meal-detail 的配料清单推断（仅供参考）
    #   "unknown"             —— 无任何过敏原信息
    allergen_source: str = "unknown"
    # 套餐拆解出的子单品名（单品则为空列表），用于营养汇总与排查
    sub_items: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 通用工具函数
# ---------------------------------------------------------------------------

def _root_causes(exc: BaseException) -> List[BaseException]:
    """递归拆解嵌套的 ExceptionGroup，取出最底层的真实异常。

    asyncio 的 TaskGroup 会把异常包成多层 ExceptionGroup，
    直接 str(e) 只能看到 "unhandled errors in a TaskGroup"，无法定位问题。
    """
    subs = getattr(exc, "exceptions", None)
    if subs:
        out: List[BaseException] = []
        for s in subs:
            out.extend(_root_causes(s))
        return out
    return [exc]


def _describe_exception(exc: BaseException, http_status: Optional[int] = None) -> str:
    """把底层异常翻译成对用户友好的中文提示。"""
    causes = _root_causes(exc)
    detail = " | ".join(f"{type(c).__name__}: {c}" for c in causes) or str(exc)

    # 优先使用 httpx 钩子捕获到的真实 HTTP 状态码（SDK 会把它吞成 JSON-RPC -32603）
    if http_status == 401 or http_status == 403:
        return (f"MCP 服务鉴权失败（HTTP {http_status}）。\n"
                "请检查 mcp-config.json 中的 token 是否为有效值（是否已过期/被重置），"
                "以及请求头格式是否为 `Authorization: Bearer <token>`。")
    if http_status is not None and http_status >= 500:
        return f"MCP 服务端异常（HTTP {http_status}），请稍后重试。\n细节：{detail}"
    if http_status is not None and http_status >= 400:
        return f"MCP 请求被拒绝（HTTP {http_status}）。\n细节：{detail}"

    lowered = detail.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return f"连接麦当劳 MCP 超时，请检查网络或代理设置。\n细节：{detail}"
    if "connect" in lowered or "nodename" in lowered or "getaddrinfo" in lowered:
        return f"无法连接麦当劳 MCP 服务，请检查网络连通性。\n细节：{detail}"
    return f"调用麦当劳 MCP 失败。\n细节：{detail}"


def _first_list(payload: Any, keys: tuple = LIST_KEYS) -> Optional[List[Any]]:
    """从 MCP 返回载荷中尽力找出"列表"主体（兼容 {"data": [...]} 等嵌套结构）。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # 只有单条记录时也包成列表，便于统一处理
        if any(k in payload for k in ("id", "product_id", "name")):
            return [payload]
    return None


def _pick_number(data: Dict[str, Any], keys: tuple, default: float = 0.0) -> float:
    """从字典中按候选键名依次查找并转为 float，找不到返回默认值。"""
    for key in keys:
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return default


def _pick_raw(data: Dict[str, Any], keys: tuple) -> Any:
    """从字典中按候选键名依次查找，返回原始值（不做类型转换）。"""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _to_text(value: Any) -> str:
    """把任意值安全地转成字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# 紧凑表格文本解析
# ---------------------------------------------------------------------------
# 麦当劳 MCP 的营养工具（list-nutrition-foods）返回的不是 JSON，而是紧凑表格文本：
#     [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
#       猪柳麦满分,null,1288,308,16,16,24,781,213
#       板烧鸡腿堡,null,1638,391,23,17,35,1041,93
# 这里把这种格式解析成 [{字段: 值}, ...]，以便与菜单按 productName 关联。
_COMPACT_HEADER_RE = re.compile(r"^\s*\[(\d+)\]\{([^}]*)\}\s*:\s*$")


def _parse_compact_table(text: Any) -> List[Dict[str, Any]]:
    """解析上述紧凑表格文本，返回记录列表；无法识别时返回空列表。"""
    if not isinstance(text, str) or not text.strip():
        return []

    lines = text.splitlines()
    header_idx, fields = None, []
    for idx, line in enumerate(lines):
        m = _COMPACT_HEADER_RE.match(line)
        if m:
            header_idx = idx
            fields = [f.strip() for f in m.group(2).split(",") if f.strip()]
            break
    if header_idx is None or not fields:
        return []

    rows: List[Dict[str, Any]] = []
    for line in lines[header_idx + 1:]:
        if not line.strip():
            continue
        # 遇到下一张表的表头就停止（一个返回体里可能有多张表）
        if _COMPACT_HEADER_RE.match(line):
            break
        values = [v.strip() for v in line.strip().split(",")]
        if len(values) != len(fields):
            continue  # 列数不符，跳过异常行
        record: Dict[str, Any] = {}
        for field, value in zip(fields, values):
            record[field] = None if value == "" or value.lower() == "null" else value
        rows.append(record)
    return rows


def _to_float(value: Any, default: float = 0.0) -> float:
    """把字符串/数字安全转换为 float。"""
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# MCP 客户端
# ---------------------------------------------------------------------------

class McpClient:
    """麦当劳 MCP 客户端（基于官方 mcp SDK · Streamable HTTP 传输）。

    用法：
        client = McpClient()
        client.list_tools()                     # 打印并返回全部工具名
        products = client.get_all_products()    # 一次性拿到全部归一化餐品
    """

    def __init__(self, config_path: Optional[str] = None, use_mock: Optional[bool] = None):
        """
        :param config_path: 自定义配置文件路径，默认读取项目根目录 mcp-config.json
        :param use_mock: 是否使用离线模拟数据（默认由环境变量 MCD_USE_MOCK 决定）。
                         仅用于无 Token 时的本地自测，正式联调请保持关闭。
        """
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.config: Dict[str, Any] = {}

        # 是否使用离线模拟数据（仅自测用，不参与正式业务流程）
        if use_mock is None:
            use_mock = os.getenv("MCD_USE_MOCK", "0") == "1"
        self.use_mock = use_mock

        # 本次连接发现的工具清单与工具↔能力映射（缓存，避免重复 tools/list）
        self.tools: List[Any] = []
        self.tool_map: Dict[str, Optional[Any]] = {}
        self._last_http_status: Optional[int] = None

        # 取数统计（供 CLI 输出"处理了多少餐品 / 多少条拿到有效数据"）
        self.stats: Dict[str, Any] = {}
        # 营养表的「名称 → 营养」索引与「归一化名称 → 营养」索引，用于宽松匹配
        self._nutrition_index: Dict[str, Dict[str, float]] = {}
        self._norm_index: Dict[str, Dict[str, float]] = {}

        if not self.use_mock:
            self.config = self._load_config()

    # ==================== 配置读取 ====================

    def _load_config(self) -> Dict[str, Any]:
        """读取配置，优先级：环境变量 > 本地配置文件 > 报错引导。"""
        config: Dict[str, Any] = {}

        # 1) 先尝试读取本地配置文件
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise McpError(f"配置文件解析失败：{self.config_path}（{exc}）") from exc
        else:
            raise McpError(
                "未找到 mcp-config.json。\n"
                f"请先复制模板：{CONFIG_EXAMPLE_PATH.name} → {self.config_path.name}\n"
                "然后填入你申请到的麦当劳 MCP Token。"
            )

        # 2) 环境变量可覆盖配置文件（便于 CI / 服务器部署）
        env_url = os.getenv("MCD_MCP_URL")
        env_token = os.getenv("MCD_MCP_TOKEN")
        if env_url:
            config["mcp_server_url"] = env_url
        if env_token:
            config["token"] = env_token

        # 3) 校验必填项
        url = str(config.get("mcp_server_url") or "").strip()
        token = str(config.get("token") or "").strip()
        if not url or url.upper().startswith("REPLACE"):
            raise McpError("mcp_server_url 未配置，请在 mcp-config.json 中填写麦当劳 MCP 服务地址。")
        if not token or token.upper().startswith("REPLACE"):
            raise McpError("token 未配置，请在 mcp-config.json 中填写真实 Token（切勿提交到 GitHub）。")

        # 4) Token 必须是 ASCII —— HTTP 请求头无法编码非 ASCII 字符。
        #    这一步能拦住「把中文占位符留在配置文件里」这类常见错误，
        #    否则会抛出难以理解的 UnicodeEncodeError。
        try:
            token.encode("ascii")
        except UnicodeEncodeError:
            raise McpError(
                "token 含有非 ASCII 字符（例如中文占位符），无法作为 HTTP 请求头发送。\n"
                "请把 mcp-config.json 中的 token 替换为你申请到的真实 Token（通常为字母数字串）。"
            ) from None

        config["mcp_server_url"] = url
        config["token"] = token
        return config

    @staticmethod
    def _mask(token: str) -> str:
        """Token 脱敏：仅保留前 4 位，用于日志打印。"""
        if not token:
            return "(空)"
        return token[:4] + "*" * max(len(token) - 4, 0)

    # ==================== 同步 / 异步桥接 ====================

    @staticmethod
    def _run(coro):
        """在同步接口中运行异步协程。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # 已在事件循环中（如 Jupyter / 异步框架）时不能嵌套 asyncio.run
        raise McpError(
            "检测到当前已处于运行中的事件循环，无法使用同步接口。\n"
            "请在异步环境中直接使用 McpClient 的 async 接口。"
        )

    # ==================== MCP 会话管理 ====================

    async def _capture_status(self, response) -> None:
        """httpx 响应钩子：记录最近一次 HTTP 状态码。

        SDK 会把 401 之类的 HTTP 错误统一包装成 JSON-RPC -32603，
        丢失状态码；这里把原始状态码捞出来，便于给出精准提示。
        """
        self._last_http_status = response.status_code

    @asynccontextmanager
    async def _session(self):
        """建立一次标准 MCP 会话：Streamable HTTP 传输 → initialize 握手。

        鉴权通过请求头 `Authorization: Bearer {token}` 携带。
        mcp SDK 为可选依赖，只有真实连接时才 import，保证 --mock 模式零依赖可用。
        """
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            # HTTP 客户端库的版本兼容导入：
            #   - mcp 2.x（如 2.3.0）依赖 httpx 的 v2 系列，包名是 `httpx2` / `httpcore2`
            #   - mcp 1.x 依赖经典 `httpx`
            # 这里不写死任一侧包名，按实际安装情况择优导入，保证在不同 mcp 版本下都能工作。
            try:
                import httpx2 as _httpx
            except ImportError:  # pragma: no cover - 兼容 mcp 1.x 环境
                import httpx as _httpx
        except ImportError as exc:  # pragma: no cover
            raise McpError(
                "未安装官方 MCP SDK，请先执行：pip install mcp\n"
                f"（原始错误：{exc}）"
            ) from exc

        url = self.config["mcp_server_url"]
        token = self.config["token"]
        timeout = float(self.config.get("timeout", 30))

        logger.debug("连接 MCP：%s（Token=%s，HTTP 客户端=%s）",
                     url, self._mask(token), _httpx.__name__)

        # 通过自定义 AsyncClient 注入鉴权请求头 + 状态码捕捉钩子
        http_client = _httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            timeout=_httpx.Timeout(timeout),
            event_hooks={"response": [self._capture_status]},
        )

        try:
            async with http_client:
                async with streamable_http_client(url, http_client=http_client) as streams:
                    # 兼容 SDK 版本差异：新版本 yield (read, write) 二元组
                    read_stream, write_stream = streams[0], streams[1]
                    async with ClientSession(read_stream, write_stream,
                                             read_timeout_seconds=timeout) as session:
                        await session.initialize()
                        logger.debug("MCP initialize 握手完成。")
                        yield session
        except McpError:
            raise
        except BaseException as exc:  # 包含 ExceptionGroup / CancelledError 等
            raise McpError(_describe_exception(exc, self._last_http_status)) from exc

    # ==================== tools/list ====================

    async def _list_tools_in_session(self, session) -> List[Any]:
        """在已建立的会话里调用 tools/list（支持分页）。"""
        tools: List[Any] = []
        cursor: Optional[str] = None
        while True:
            params = None
            if cursor:
                from mcp import types
                params = types.PaginatedRequestParams(cursor=cursor)
            result = await session.list_tools(params=params) if params else await session.list_tools()
            tools.extend(list(getattr(result, "tools", []) or []))
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                break
        return tools

    @staticmethod
    def _tool_name(tool) -> str:
        """兼容 dict / pydantic 对象两种工具描述形态，取出工具名。"""
        if isinstance(tool, dict):
            return str(tool.get("name") or "")
        return str(getattr(tool, "name", "") or "")

    @staticmethod
    def _tool_desc(tool) -> str:
        """取出工具描述文本。"""
        if isinstance(tool, dict):
            return str(tool.get("description") or "")
        return str(getattr(tool, "description", "") or "")

    @staticmethod
    def _tool_schema(tool) -> Dict[str, Any]:
        """取出工具的输入参数 schema。"""
        if isinstance(tool, dict):
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        else:
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        return schema if isinstance(schema, dict) else {}

    def _log_tool_list(self, tools: List[Any]) -> None:
        """打印 tools/list 返回的全部工具名称与参数摘要。"""
        print(f"\n[tools/list] 麦当劳 MCP 共提供 {len(tools)} 个工具：")
        for idx, tool in enumerate(tools, start=1):
            name = self._tool_name(tool)
            desc = self._tool_desc(tool)[:60].replace("\n", " ")
            props = list(self._tool_schema(tool).get("properties", {}).keys())
            print(f"  {idx:>2}. {name}")
            if desc:
                print(f"      └─ 说明：{desc}")
            if props:
                print(f"      └─ 参数：{', '.join(props)}")

    def list_tools(self, print_result: bool = True) -> List[str]:
        """调用 tools/list，打印并返回全部工具名（同步接口）。"""
        if self.use_mock:
            names = ["mock_get_menu", "mock_get_nutrition", "mock_get_allergens", "mock_get_price"]
            if print_result:
                print("\n[mock 模式] 未连接真实 MCP，工具列表为本地模拟：")
                for n in names:
                    print(f"    - {n}")
            return names

        tools = self._run(self._async_list_tools())
        if print_result:
            self._log_tool_list(tools)
        return [self._tool_name(t) for t in tools]

    async def _async_list_tools(self) -> List[Any]:
        """异步获取工具列表。"""
        async with self._session() as session:
            tools = await self._list_tools_in_session(session)
        self.tools = tools
        self.tool_map = self._discover_tool_map(tools)
        return tools

    # ==================== 工具发现与参数构造 ====================

    def _discover_tool_map(self, tools: List[Any]) -> Dict[str, Optional[Any]]:
        """把「菜单 / 营养 / 过敏原 / 价格」四类能力映射到具体 MCP 工具。

        匹配优先级：
            1. mcp-config.json 中的 "tool_map"（手动指定，最精确）
            2. DEFAULT_TOOL_MAP（依据实测的麦当劳 MCP 工具名）
            3. 关键词打分自动匹配（兜底，便于适配 MCP 工具改名）
        """
        manual = self.config.get("tool_map") or {}
        names_on_server = {self._tool_name(t) for t in tools}
        mapping: Dict[str, Optional[Any]] = {}

        for cap, hints in TOOL_HINTS.items():
            # 1) 手动指定优先
            pinned = manual.get(cap) or DEFAULT_TOOL_MAP.get(cap)
            if pinned:
                hit = next((t for t in tools if self._tool_name(t) == pinned), None)
                if hit is not None:
                    mapping[cap] = hit
                    continue

            # 2) 自动打分匹配
            best, best_score = None, 0.0
            for tool in tools:
                name = self._tool_name(tool).lower()
                desc = self._tool_desc(tool).lower()
                score = 0.0
                for hint in hints:
                    hint_l = hint.lower()
                    if hint_l == name:
                        score += 5.0          # 工具名完全一致，权重最高
                    elif hint_l in name:
                        score += 3.0          # 工具名部分命中
                    elif hint_l in desc:
                        score += 1.0          # 描述命中，权重低
                if score > best_score:
                    best, best_score = tool, score
            mapping[cap] = best

        # 记录一次映射结果，便于排查「为什么取不到数据」
        logger.debug("工具映射结果：%s", {k: self._tool_name(v) if v else None
                                          for k, v in mapping.items()})
        if mapping.get("allergen") is None:
            logger.warning("未在该 MCP 中匹配到「过敏原」工具（服务端共 %s 个工具）。"
                           "所有餐品的过敏原将按「未知」处理，推荐引擎会执行安全优先过滤。",
                           len(names_on_server))
        return mapping

    def _build_arguments(self, tool, keyword: str = "", product_id: str = "") -> Dict[str, Any]:
        """依据工具的 inputSchema 构造调用参数。

        麦当劳 MCP 的菜单/价格类工具属于「门店上下文」接口，必填
        storeCode / orderType / beType 等参数（见 query-meals 的工具说明），
        这些值从 mcp-config.json 读取：

            {
              "store_code": "1450713",   // 门店编码，可用门店查询工具获取
              "order_type": 1,           // 1-到店（含自取/得来速） 2-外送
              "be_type": 1,              // 1-到店取餐 2-麦乐送 5-得来速 6-企业团餐
              "be_code": ""              // 得来速/外送场景必填
            }

        其余参数按名称语义自动填充：
            - 形如 id / code / sku 的参数 → 填入 product_id
            - 形如 query / keyword / name 的参数 → 填入 keyword
            - schema 中带 default 的参数 → 使用默认值
        """
        schema = self._tool_schema(tool)
        properties: Dict[str, Any] = schema.get("properties", {}) or {}
        args: Dict[str, Any] = {}

        # 门店上下文字段：名称 → 配置文件键名
        context_keys = {
            "storecode": "store_code",
            "ordertype": "order_type",
            "betype": "be_type",
            "becode": "be_code",
        }

        id_words = ("id", "code", "sku", "no")
        kw_words = ("query", "keyword", "search", "name", "text", "key")

        for prop, spec in properties.items():
            low = prop.lower()

            # 1) 门店上下文参数优先（否则必填缺失会导致调用失败）
            #    注意：无论是否取到值都必须 continue，不能让上下文参数掉进下面的
            #    通用填充逻辑——否则 beCode 会因为名字里含 "code" 而被误填成商品码
            #    （到店自取场景本应「不传 beCode」）。
            if low in context_keys:
                value = self.config.get(context_keys[low])
                if value not in (None, ""):
                    args[prop] = value
                continue

            # 2) 商品标识
            if product_id and any(w in low for w in id_words):
                args[prop] = product_id
            # 3) 关键词
            elif keyword and any(w in low for w in kw_words):
                args[prop] = keyword
            # 4) schema 默认值
            elif "default" in (spec or {}):
                args[prop] = spec["default"]
        return args

    # ==================== tools/call ====================

    @staticmethod
    def _extract_payload(result: Any) -> Any:
        """从 CallToolResult 中提取可用载荷。

        优先取结构化内容（structuredContent）；
        否则把文本内容拼接后尝试 JSON 解析，解析失败则原样返回文本。
        """
        if result is None:
            return None
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            text = _to_text(getattr(result, "content", ""))
            logger.error("MCP 工具返回错误：%s", text[:300])
            return None

        structured = getattr(result, "structured_content", None)
        if structured is None:
            structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured

        # 回退：拼接 TextContent 文本
        texts: List[str] = []
        for item in getattr(result, "content", None) or []:
            if isinstance(item, dict):
                texts.append(_to_text(item.get("text")))
            else:
                texts.append(_to_text(getattr(item, "text", "")))
        raw = "\n".join(t for t in texts if t).strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw

    async def _call_in_session(self, session, tool, arguments: Dict[str, Any]) -> Any:
        """在已建立的会话里执行一次 tools/call。"""
        if tool is None:
            return None
        name = self._tool_name(tool)
        logger.debug("调用工具 %s，参数 %s", name, arguments)
        result = await session.call_tool(name, arguments or {})
        return self._extract_payload(result)

    async def _async_call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """独立会话执行一次 tools/call。"""
        async with self._session() as session:
            tools = await self._list_tools_in_session(session)
            self.tools = tools
            self.tool_map = self._discover_tool_map(tools)
            tool = next((t for t in tools if self._tool_name(t) == name), None)
            if tool is None:
                raise McpError(f"MCP 未提供名为 {name} 的工具，请先用 list_tools() 查看可用工具。")
            return await self._call_in_session(session, tool, arguments or {})

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """通用工具调用接口（同步）。"""
        if self.use_mock:
            raise McpError("mock 模式下请使用 get_all_products()，不要直接 call_tool()。")
        return self._run(self._async_call_tool(name, arguments))

    # ==================== 结果归一化 ====================

    def _normalize_menu(self, payload: Any) -> List[Dict[str, Any]]:
        """把菜单工具返回结果归一化为 [{product_id, name, category, price}]。

        兼容两种真实形态：
          A) 麦当劳 MCP `query-meals` 的返回：
             {"categories":[{"name":"人气热卖","meals":[{"code":"521954"}]}],
              "meals":{"521954":{"name":"板烧鸡腿堡","currentPrice":"20"}}}
             —— 餐品名与价格在 meals 字典里，分类关系在 categories 里
          B) 通用列表形态：[{"id":..., "name":..., "price":...}, ...]
        """
        if not isinstance(payload, dict):
            # 形态 B：直接是列表
            items = _first_list(payload)
            return self._normalize_menu_list(items)

        # 形态 A：query-meals
        meals = payload.get("meals")
        if isinstance(meals, dict) and meals:
            # code → 分类名
            code_to_category: Dict[str, str] = {}
            for cat in payload.get("categories") or []:
                if not isinstance(cat, dict):
                    continue
                cat_name = str(cat.get("name") or "未分类")
                for meal in cat.get("meals") or []:
                    if isinstance(meal, dict) and meal.get("code"):
                        code_to_category.setdefault(str(meal["code"]), cat_name)

            out: List[Dict[str, Any]] = []
            for code, info in meals.items():
                if not isinstance(info, dict):
                    continue
                out.append({
                    "product_id": str(code),
                    "name": str(info.get("name") or "未知餐品"),
                    "category": code_to_category.get(str(code), "未分类"),
                    "price": _to_float(info.get("currentPrice") or info.get("originalPrice")),
                })
            return out

        # 形态 B（被包在 data 里）
        for container in ("data", "result"):
            inner = payload.get(container)
            if isinstance(inner, (list, dict)):
                found = self._normalize_menu(inner)
                if found:
                    return found
        return self._normalize_menu_list(_first_list(payload))

    def _normalize_menu_list(self, items: Any) -> List[Dict[str, Any]]:
        """归一化通用列表形态的菜单。"""
        if not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = (item.get("id") or item.get("code") or item.get("product_id")
                   or item.get("productId") or item.get("sku"))
            if not pid:
                continue
            category = item.get("category") or item.get("type") or item.get("categoryName") or "未分类"
            if isinstance(category, dict):
                category = category.get("name") or "未分类"
            out.append({
                "product_id": str(pid),
                "name": str(item.get("name") or item.get("title") or item.get("productName") or "未知餐品"),
                "category": str(category),
                "price": _to_float(item.get("currentPrice") or item.get("price") or item.get("unitPrice")),
            })
        return out

    def _normalize_nutrition(self, payload: Any) -> Dict[str, float]:
        """把营养工具返回结果归一化为 {calories, protein, fat, carbs, sodium}。"""
        if payload is None:
            return {}
        data = payload
        if isinstance(data, dict):
            # 可能被包在 data / nutrition 里
            for key in ("data", "nutrition", "result", "nutrients"):
                if isinstance(data.get(key), (dict, list)):
                    data = data[key]
                    break
        if isinstance(data, list):
            if not data:
                return {}
            data = data[0]
        if not isinstance(data, dict):
            return {}
        return {name: _pick_number(data, keys) for name, keys in NUTRITION_KEYS.items()}

    def _normalize_allergens(self, payload: Any) -> Optional[Set[str]]:
        """把过敏原工具返回结果归一化为过敏原集合。

        返回 None 表示"数据缺失/无法解析"（推荐引擎将按安全优先策略过滤）；
        返回空集合表示"确认无已知过敏原"。
        """
        if payload is None:
            return None

        raw: Any = None
        if isinstance(payload, list):
            raw = payload
        elif isinstance(payload, dict):
            for key in ALLERGEN_KEYS:
                if key in payload:
                    raw = payload[key]
                    break
            else:
                for container in ("data", "result"):
                    inner = payload.get(container)
                    if isinstance(inner, dict):
                        for key in ALLERGEN_KEYS:
                            if key in inner:
                                raw = inner[key]
                                break
                    elif isinstance(inner, list):
                        raw = inner
                    if raw is not None:
                        break

        if raw is None:
            return None  # 字段缺失 → 未知
        if isinstance(raw, str):
            # 形如 "花生,牛奶" 或 "peanuts; milk"
            parts = [p.strip() for p in raw.replace("；", ",").replace(";", ",").split(",")]
            return {p.lower() for p in parts if p}
        if isinstance(raw, list):
            out: Set[str] = set()
            for item in raw:
                if isinstance(item, dict):
                    value = item.get("name") or item.get("code") or item.get("allergen")
                else:
                    value = item
                if value:
                    out.add(str(value).strip().lower())
            return out
        return None

    @staticmethod
    def _normalize_price(payload: Any) -> float:
        """把价格工具返回结果归一化为 float（元）。"""
        if payload is None:
            return 0.0
        if isinstance(payload, (int, float)):
            return float(payload)
        data = payload
        if isinstance(data, dict):
            for key in ("data", "result", "price"):
                if key in data and isinstance(data[key], (dict, int, float)):
                    data = data[key]
                    break
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            return _pick_number(data, PRICE_KEYS)
        return 0.0

    # ==================== 对外业务接口 ====================

    def get_menu(self) -> List[Dict[str, Any]]:
        """获取餐品列表（tools/call · 菜单工具）。"""
        if self.use_mock:
            return self._mock_menu()
        payload = self._run(self._async_call_capability("menu"))
        return self._normalize_menu(payload)

    def get_nutrition(self, product_id: str) -> Dict[str, float]:
        """获取单品营养信息（tools/call · 营养工具）。"""
        if self.use_mock:
            return self._mock_nutrition(product_id)
        payload = self._run(self._async_call_capability("nutrition", product_id=product_id))
        return self._normalize_nutrition(payload)

    def get_allergens(self, product_id: str) -> Optional[Set[str]]:
        """获取单品过敏原集合（tools/call · 过敏原工具）。"""
        if self.use_mock:
            return self._mock_allergens(product_id)
        payload = self._run(self._async_call_capability("allergen", product_id=product_id))
        return self._normalize_allergens(payload)

    def get_price(self, product_id: str) -> float:
        """获取单品价格（tools/call · 价格工具）。"""
        if self.use_mock:
            return self._mock_price(product_id)
        payload = self._run(self._async_call_capability("price", product_id=product_id))
        return self._normalize_price(payload)

    async def _async_call_capability(self, capability: str, product_id: str = "",
                                     keyword: str = "") -> Any:
        """按能力名（menu/nutrition/allergen/price）调用对应 MCP 工具。"""
        async with self._session() as session:
            tools = await self._list_tools_in_session(session)
            self.tools = tools
            self.tool_map = self._discover_tool_map(tools)
            tool = self.tool_map.get(capability)
            if tool is None:
                logger.warning("未能匹配到「%s」对应的 MCP 工具，返回空结果。可用工具：%s",
                               capability, [self._tool_name(t) for t in tools])
                return None
            args = self._build_arguments(tool, keyword=keyword, product_id=product_id)
            return await self._call_in_session(session, tool, args)

    def _build_nutrition_index(self, payload: Any) -> Dict[str, Dict[str, float]]:
        """把营养工具返回结果整理成「餐品名 → 营养字典」的索引。

        真实形态：list-nutrition-foods 返回紧凑表格文本，字段为
            productName / nutritionDescription / energyKj / energyKcal /
            protein / fat / carbohydrate / sodium / calcium
        由于该表以「餐品名」为主键，因此与菜单按名称关联。
        """
        records: List[Dict[str, Any]] = []

        def _collect(node: Any) -> None:
            if node is None:
                return
            if isinstance(node, str):
                records.extend(_parse_compact_table(node))
            elif isinstance(node, list):
                for entry in node:
                    if isinstance(entry, dict):
                        records.append(entry)
            elif isinstance(node, dict):
                # 优先往下钻到 data / result / nutrition
                nested = False
                for key in ("data", "result", "nutrition", "foods", "list"):
                    if key in node:
                        _collect(node[key])
                        nested = True
                if not nested:
                    records.append(node)

        _collect(payload)

        index: Dict[str, Dict[str, float]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            name = (record.get("productName") or record.get("name")
                    or record.get("product_name") or record.get("餐品名"))
            if not name:
                continue
            index[str(name).strip()] = {
                field: _to_float(_pick_raw(record, keys))
                for field, keys in NUTRITION_KEYS.items()
            }
        return index

    # ==================== 方案B：套餐拆解 / 配料推断 / 营养匹配 ====================

    @staticmethod
    def _extract_sub_items(detail: Dict[str, Any]) -> List[str]:
        """从 query-meal-detail 返回中拆解出套餐内的子单品名。

        套餐的 rounds[].choices[] 描述每个「选择轮次」下的可选项，
        取其中 isDefault=1 或 quantity>0 的项即为默认搭配，
        例如「麦辣鸡腿汉堡三件套」→ [麦辣鸡腿汉堡, 中薯条, 可乐中杯]。

        单品（rounds 为空）返回空列表，由调用方回退为餐品自身名称。
        """
        names: List[str] = []
        for rnd in detail.get("rounds") or []:
            if not isinstance(rnd, dict):
                continue
            for choice in rnd.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                # 优先取默认项；若没有默认标记，则取数量 > 0 的项
                if choice.get("isDefault") == 1 or _to_float(choice.get("quantity")) > 0:
                    name = choice.get("name")
                    if name:
                        names.append(str(name))
                    break
        return names

    @staticmethod
    def _extract_ingredients(detail: Dict[str, Any]) -> List[str]:
        """从 query-meal-detail 的配料清单中提取实际配料名。

        modification.items[].values[] 为可特调配料，取 selectedQuantity>0 的项。
        注意：这是「可特调项」而非完整配料表，必然不完整。
        """
        names: List[str] = []
        modification = detail.get("modification") or {}
        for item in modification.get("items") or []:
            if not isinstance(item, dict):
                continue
            for value in item.get("values") or []:
                if not isinstance(value, dict):
                    continue
                if _to_float(value.get("selectedQuantity")) > 0 and value.get("name"):
                    names.append(str(value["name"]))
        return names

    @staticmethod
    def _infer_allergens_from_ingredients(ingredients: List[str]) -> Set[str]:
        """由配料名推断疑似过敏原（方案B · 配料表推断）。

        ⚠️ 启发式推断，仅用于**标记风险**，绝不用于声明「不含过敏原」。
        """
        hits: Set[str] = set()
        for ingredient in ingredients:
            for allergen, keywords in INGREDIENT_ALLERGEN_KEYWORDS.items():
                if any(keyword in ingredient for keyword in keywords):
                    hits.add(allergen)
        return hits

    @staticmethod
    def _normalize_product_name(name: str) -> str:
        """餐品名归一化：去空格 / 去括号后缀 / 去商标符号，便于宽松匹配。"""
        text = str(name or "").strip()
        text = re.sub(r"[（(][^）)]*[）)]", "", text)   # 去掉括号及其中内容
        text = text.replace("™", "").replace("®", "").replace(" ", "")
        return text

    def _lookup_nutrition(self, name: str) -> Optional[Dict[str, float]]:
        """按名称查营养表：精确匹配 → 归一化匹配 → 唯一包含匹配。

        包含匹配仅在「最长命中唯一」时采纳，避免「薯条」误配到多个规格。
        """
        if not name:
            return None
        if name in self._nutrition_index:
            return self._nutrition_index[name]

        normalized = self._normalize_product_name(name)
        if normalized and normalized in self._norm_index:
            return self._norm_index[normalized]

        if len(normalized) >= 3:
            candidates = [
                (len(nut_name), value)
                for nut_name, value in self._norm_index.items()
                if len(nut_name) >= 3 and (nut_name in normalized or normalized in nut_name)
            ]
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                longest = candidates[0][0]
                tied = [c for c in candidates if c[0] == longest]
                if len(tied) == 1:      # 最长命中唯一才采纳
                    return tied[0][1]
        return None

    def get_all_products(self) -> List[Product]:
        """批量拉取并归一化全部餐品（单次会话内完成，避免重复握手）。"""
        if self.use_mock:
            return self._mock_products()
        return self._run(self._async_get_all_products())

    async def _async_get_all_products(self) -> List[Product]:
        """方案B 主流程：菜单 → 逐条餐品详情（拆解套餐 + 提取配料）→ 营养汇总。

        步骤：
          1) tools/list 发现工具
          2) tools/call · query-meals          取菜单（code + 名称 + 价格）
          3) tools/call · list-nutrition-foods 取营养表（营养数据仅此一处）
          4) tools/call · query-meal-detail    逐条取详情：
               - 套餐 → 拆解出子单品 → 用子单品名去营养表匹配 → 汇总营养
               - 单品 → 直接用自身名称匹配
               - 并从配料清单推断疑似过敏原
        """
        async with self._session() as session:
            # 1) tools/list
            tools = await self._list_tools_in_session(session)
            self.tools = tools
            self.tool_map = self._discover_tool_map(tools)

            # 2) tools/call · 菜单列表
            menu_tool = self.tool_map.get("menu")
            if menu_tool is None:
                logger.error("未能匹配到菜单工具，可用工具：%s",
                             [self._tool_name(t) for t in tools])
                return []
            menu_payload = await self._call_in_session(
                session, menu_tool, self._build_arguments(menu_tool)
            )
            menu = self._normalize_menu(menu_payload)
            if not menu:
                logger.error("菜单工具返回结果为空或结构未识别。")
                return []
            logger.info("菜单工具 %s 返回 %s 款餐品。", self._tool_name(menu_tool), len(menu))

            # 3) tools/call · 营养表（营养数据的唯一来源，按名称建索引）
            nutrition_tool = self.tool_map.get("nutrition")
            if nutrition_tool is not None:
                nutrition_payload = await self._call_in_session(
                    session, nutrition_tool, self._build_arguments(nutrition_tool)
                )
                self._nutrition_index = self._build_nutrition_index(nutrition_payload)
                self._norm_index = {
                    self._normalize_product_name(k): v
                    for k, v in self._nutrition_index.items()
                }
                logger.info("营养工具 %s 提供 %s 条营养数据（作为营养与热量数据源）。",
                            self._tool_name(nutrition_tool), len(self._nutrition_index))
            else:
                self._nutrition_index = {}
                self._norm_index = {}
                logger.warning("未匹配到营养工具，营养字段将全部为 0。")

            # 4) tools/call · 逐条餐品详情（方案B 核心）
            detail_tool = next(
                (t for t in tools if self._tool_name(t) == "query-meal-detail"), None
            )
            if detail_tool is None:
                detail_tool = self.tool_map.get("detail")
            price_tool = self.tool_map.get("price")
            allergen_tool = self.tool_map.get("allergen")

            total = len(menu)
            progress_every = int(self.config.get("progress_every", 20) or 20)

            products: List[Product] = []
            # 统计项
            nutrition_ok = 0          # 成功拿到有效营养数据的餐品数
            allergen_inferred = 0     # 由配料推断出疑似过敏原的餐品数
            allergen_authoritative = 0  # 拿到权威过敏原数据的餐品数
            combo_count = 0
            detail_fail = 0

            for idx, item in enumerate(menu, start=1):
                name = item["name"]
                pid = item["product_id"]

                # --- 4.1 取餐品详情（套餐拆解 + 配料提取）---
                detail: Dict[str, Any] = {}
                if detail_tool is not None:
                    detail_args = self._build_arguments(detail_tool, product_id=pid, keyword=name)
                    # query-meal-detail 的商品参数名是 code（非 id），此处显式补齐
                    schema_props = self._tool_schema(detail_tool).get("properties", {}) or {}
                    if "code" in schema_props:
                        detail_args["code"] = pid
                    detail_payload = await self._call_in_session(session, detail_tool, detail_args)
                    if isinstance(detail_payload, dict):
                        inner = detail_payload.get("data")
                        detail = inner if isinstance(inner, dict) else detail_payload
                    if not detail:
                        detail_fail += 1

                sub_items = self._extract_sub_items(detail)
                if sub_items:
                    combo_count += 1
                # 单品：用自身名称参与营养匹配
                lookup_names = sub_items or [name]

                # --- 4.2 营养汇总（套餐 = 子单品营养之和）---
                nutrition = {field: 0.0 for field in NUTRITION_KEYS}
                matched_any = False
                for sub_name in lookup_names:
                    found = self._lookup_nutrition(sub_name)
                    if found:
                        matched_any = True
                        for field in NUTRITION_KEYS:
                            nutrition[field] += _to_float(found.get(field))
                if matched_any:
                    nutrition_ok += 1

                # --- 4.3 过敏原：权威数据（若有工具）> 配料推断 ---
                allergens: Optional[Set[str]] = None
                if allergen_tool is not None:
                    allergen_payload = await self._call_in_session(
                        session, allergen_tool,
                        self._build_arguments(allergen_tool, product_id=pid, keyword=name),
                    )
                    allergens = self._normalize_allergens(allergen_payload)

                if allergens is not None:
                    allergen_source = "mcp"
                    allergen_authoritative += 1
                else:
                    # 方案B：由配料清单推断疑似过敏原（仅供参考，不视为权威）
                    inferred = self._infer_allergens_from_ingredients(
                        self._extract_ingredients(detail)
                    )
                    allergens = inferred
                    allergen_source = "ingredient_inferred" if inferred else "unknown"
                    if inferred:
                        allergen_inferred += 1

                # --- 4.4 价格：菜单已带 currentPrice，缺失时用价格工具兜底 ---
                price = _to_float(item.get("price"))
                if not price and price_tool is not None and price_tool is not menu_tool:
                    price_payload = await self._call_in_session(
                        session, price_tool,
                        self._build_arguments(price_tool, product_id=pid, keyword=name),
                    )
                    price = self._normalize_price(price_payload)

                products.append(Product(
                    product_id=pid,
                    name=name,
                    category=item["category"],
                    calories=nutrition.get("calories", 0.0),
                    protein=nutrition.get("protein", 0.0),
                    fat=nutrition.get("fat", 0.0),
                    carbs=nutrition.get("carbs", 0.0),
                    sodium=nutrition.get("sodium", 0.0),
                    price=price,
                    allergens=allergens or set(),
                    allergen_known=(allergen_source == "mcp"),
                    allergen_source=allergen_source,
                    sub_items=sub_items,
                ))

                # --- 4.5 进度日志：已处理 / 总数 ---
                logger.debug("已处理 %s/%s 餐品：%s（子单品 %s 个，营养命中 %s）",
                             idx, total, name, len(sub_items), matched_any)
                if idx % progress_every == 0 or idx == total:
                    print(f"[进度] 已处理 {idx}/{total} 餐品 ...", flush=True)

            # 5) 汇总统计，供 CLI 输出
            self.stats = {
                "total": total,
                "combo": combo_count,
                "single": total - combo_count,
                "nutrition_ok": nutrition_ok,
                "allergen_authoritative": allergen_authoritative,
                "allergen_inferred": allergen_inferred,
                "detail_fail": detail_fail,
                "nutrition_table_size": len(self._nutrition_index),
            }
            logger.info("处理完成：共 %s 款餐品（套餐 %s / 单品 %s）",
                        total, combo_count, total - combo_count)
            logger.info("营养数据命中 %s/%s 款；过敏原权威数据 %s 款，配料推断 %s 款",
                        nutrition_ok, total, allergen_authoritative, allergen_inferred)
            if detail_fail:
                logger.warning("有 %s 款餐品详情获取失败（已按无子单品/无配料处理）。", detail_fail)
            if allergen_authoritative == 0:
                logger.warning("该 MCP 未提供权威过敏原数据；当前过敏原来自「配料推断」，"
                               "仅供参考，不可作为「不含过敏原」的依据。详见 MCP_INTEGRATION.md。")
            return products

    # ==================== 离线模拟数据（仅自测用） ====================
    # 注意：以下数据仅供无 Token 时本地跑通流程，正式演示与联调请保持 use_mock=False。

    _MOCK_DATA = [
        # (id, 名称, 分类, 热量, 蛋白, 脂肪, 碳水, 钠, 价格, 过敏原)
        ("M001", "板烧鸡腿堡", "汉堡", 430, 27.0, 17.0, 40.0, 880, 20.0, ["gluten", "soy"]),
        ("M002", "巨无霸", "汉堡", 550, 25.0, 29.0, 45.0, 950, 24.0, ["gluten", "soy", "egg"]),
        ("M003", "麦香鸡汉堡", "汉堡", 400, 20.0, 18.0, 38.0, 780, 17.0, ["gluten", "soy"]),
        ("M004", "麦辣鸡腿堡", "汉堡", 490, 24.0, 22.0, 42.0, 1020, 21.0, ["gluten", "soy", "milk"]),
        ("M005", "无糖冰红茶", "饮料", 0, 0.0, 0.0, 0.0, 5, 6.0, []),
        ("M006", "可口可乐（中）", "饮料", 150, 0.0, 0.0, 38.0, 30, 8.0, []),
        ("M007", "玉米杯", "小食", 90, 3.0, 1.0, 19.0, 0, 7.0, []),
        ("M008", "薯条（中）", "小食", 340, 4.0, 16.0, 44.0, 260, 11.0, []),
        ("M009", "麦乐鸡（5块）", "小食", 220, 13.0, 13.0, 12.0, 460, 13.0, ["gluten", "soy"]),
        ("M010", "花生圣代", "甜品", 320, 6.0, 12.0, 48.0, 120, 10.0, ["milk", "peanuts"]),
        ("M011", "原味甜筒", "甜品", 200, 5.0, 6.0, 32.0, 90, 6.0, ["milk", "gluten"]),
        ("M012", "牛奶（纯牛奶）", "饮料", 130, 7.0, 5.0, 12.0, 100, 8.0, ["milk"]),
    ]

    def _mock_menu(self) -> List[Dict[str, Any]]:
        return [{"product_id": r[0], "name": r[1], "category": r[2]} for r in self._MOCK_DATA]

    def _mock_nutrition(self, product_id: str) -> Dict[str, float]:
        row = self._mock_row(product_id)
        if not row:
            return {}
        return {"calories": row[3], "protein": row[4], "fat": row[5], "carbs": row[6], "sodium": row[7]}

    def _mock_allergens(self, product_id: str) -> Optional[Set[str]]:
        row = self._mock_row(product_id)
        if row is None:
            return None
        return set(row[9])

    def _mock_price(self, product_id: str) -> float:
        row = self._mock_row(product_id)
        return float(row[8]) if row else 0.0

    def _mock_products(self) -> List[Product]:
        """离线模式下一次性产出全部 Product（等价于真实 get_all_products 的结果）。"""
        products: List[Product] = []
        for row in self._MOCK_DATA:
            products.append(Product(
                product_id=row[0], name=row[1], category=row[2],
                calories=float(row[3]), protein=float(row[4]), fat=float(row[5]),
                carbs=float(row[6]), sodium=float(row[7]), price=float(row[8]),
                allergens=set(row[9]), allergen_known=True,
                allergen_source="mcp", sub_items=[],
            ))
        logger.info("成功归一化 %s 款餐品（mock）。", len(products))
        self.stats = {
            "total": len(products),
            "combo": 0,
            "single": len(products),
            "nutrition_ok": len(products),
            "allergen_authoritative": len(products),
            "allergen_inferred": 0,
            "detail_fail": 0,
            "nutrition_table_size": len(products),
        }
        return products

    def _mock_row(self, product_id: str):
        for row in self._MOCK_DATA:
            if row[0] == product_id:
                return row
        return None
