# MCP 接入文档 · MCP_INTEGRATION.md

> 本文档说明 McHealth Agent 如何接入麦当劳 MCP 服务，包含**协议说明、接口清单、调用流程、实测结论、密钥安全方案、WorkBuddy 开发说明、异常处理**七部分。

---

## 〇、协议说明（重要）

麦当劳 MCP（`https://mcp.mcd.cn`）使用的是**标准 MCP 协议（Model Context Protocol，Streamable HTTP 传输）**，
**不是普通 REST 接口**。因此：

| | 错误做法 ❌ | 正确做法 ✅ |
| --- | --- | --- |
| 调用方式 | `requests.get("/menu")` 这类 REST 路径 | 官方 `mcp` Python SDK 建立会话 |
| 发现能力 | 靠文档硬编码接口路径 | 先 `tools/list` 拿到工具清单 |
| 执行调用 | 拼 URL + 查询参数 | `tools/call` 传工具名 + 结构化参数 |
| 鉴权 | 自定义 header | 会话请求头 `Authorization: Bearer {token}` |

本项目 `src/mcp_client.py` 已完全按上述方式重写，依赖官方 SDK：

```bash
pip install "mcp>=1.0.0"
```

---

## 一、调用接口清单

`src/mcp_client.py` 对外保持 4 类业务能力的稳定 API，内部通过 MCP 工具实现。

| 编号 | 对外方法 | 映射的 MCP 工具 | 用途 | 入参 |
| --- | --- | --- | --- | --- |
| 1 | `get_menu()` | `query-meals` | 餐品列表（含名称与价格） | `storeCode` `orderType` `beType` |
| 2 | `get_nutrition(product_id)` | `list-nutrition-foods` | 营养成分表（**营养数据的唯一来源**） | 无（全局表，按餐品名关联） |
| 3 | `get_allergens(product_id)` | ⚠️ **无对应工具** | 过敏原信息（改由配料清单推断，见方案B） | — |
| 4 | `get_price(product_id)` | `query-meals`（`currentPrice`） | 单价 | 同 1 |
| 5 | `get_all_products()` 内部 | `query-meal-detail` | **套餐拆解** + 提取配料清单 | `storeCode` `code` `orderType` `beType` |

此外提供：

| 方法 | 说明 |
| --- | --- |
| `list_tools()` | 调用 `tools/list`，打印并返回麦当劳 MCP 提供的全部工具名 |
| `get_all_products()` | 单次会话内完成「菜单 + 营养 + 价格」拉取与归一化，产出 `Product` 列表 |
| `call_tool(name, args)` | 通用 `tools/call` 入口，用于调用未封装的工具 |

> 能力到工具的映射默认使用内置表 `DEFAULT_TOOL_MAP`；
> 若 MCP 工具改名，可在 `mcp-config.json` 的 `tool_map` 中手动指定；
> 仍未命中时会退化为关键词自动匹配。

---

## 二、调用流程

```text
① 读取 mcp-config.json（本地密钥文件，含 token 与门店上下文）
        │
        ▼
② 建立 MCP 会话：Streamable HTTP 传输 + Authorization: Bearer {token}
        │
        ▼
③ initialize 握手（协商协议版本与能力）
        │
        ▼
④ tools/list  ──────────────►  获取全部工具清单（本项目实测为 35 个）
        │
        ▼
⑤ 工具映射：菜单 → query-meals，营养 → list-nutrition-foods，价格 → query-meals
        │
        ▼
⑥ tools/call · query-meals（传 storeCode/orderType/beType）
        │        └─► 返回 {categories:[...], meals:{code:{name, currentPrice}}}
        ▼
⑦ tools/call · list-nutrition-foods（无参数）
        │        └─► 返回紧凑表格文本，含 energyKcal/protein/fat/carbohydrate/sodium
        ▼
⑧ 按「餐品名」把营养表关联到菜单，归一化为 Product 列表
        │
        ▼
⑨ 交给 recommender.py：过敏原过滤 → 热量/预算筛选 → 营养汇总
        │
        ▼
⑩ main.py 打印结构化结果
```

### 真实调用时序

```text
main.py          recommender.py        mcp_client.py        麦当劳MCP
   │                   │                    │                   │
   │                   │                    │  streamable_http  │
   │                   │                    ├──────────────────►│
   │                   │                    │  initialize       │
   │                   │                    ├──────────────────►│
   │                   │                    │  tools/list (35)  │
   │                   │                    ├──────────────────►│
   │                   │                    │  tools/call       │
   │                   │                    │  query-meals      │
   │                   │                    ├──────────────────►│
   │                   │                    │  list-nutrition   │
   │                   │                    ├──────────────────►│
   │                   │◄───────────────────┤                   │
   │                   │ 过滤 + 筛选 + 汇总   │                   │
   │◄──────────────────┤                    │                   │
```

---

## 三、实测结论（基于真实端点 `https://mcp.mcd.cn`）

> 以下结论由本项目实际调用麦当劳 MCP 得到，是接入的一手依据。

### 1. 工具总数：35 个

与「点餐 + 会员 + 商城 + 派对活动」相关，例如：

| 工具名 | 用途 |
| --- | --- |
| `query-meals` | 餐品列表（必填 `storeCode` `orderType` `beType`） |
| `query-meal-detail` | 餐品详情（套餐组成 / 特调选项） |
| `list-nutrition-foods` | **营养成分表**（无参数，160 条） |
| `calculate-price` | 价格试算（含优惠） |
| `query-nearby-stores` | 到店场景门店查询（取 `storeCode`） |
| `create-order` / `cancel-order` | 下单 / 取消（**本 MVP 不调用**） |

### 2. 门店上下文是前置条件

`query-meals`、`query-meal-detail`、`calculate-price`、`query-store-coupons` 等
**都必须带门店标识**，否则无法调用：

| 参数 | 说明 |
| --- | --- |
| `storeCode` | 门店编码，必填；可用 `query-nearby-stores` 查询获得 |
| `orderType` | `1`-到店（含自取/得来速）　`2`-外送（含麦乐送/团餐） |
| `beType` | `1`-到店取餐　`2`-麦乐送　`5`-得来速　`6`-企业团餐 |
| `beCode` | 得来速/外送/团餐场景必填；到店自取不传 |

因此这些值放在 `mcp-config.json` 中统一配置。

### 3. 返回数据形态

- `query-meals` → JSON：`{"categories":[...], "meals":{"<code>":{"name":..., "currentPrice":...}}}`
  —— **一次即可拿到餐品名与价格**，无需再调 `calculate-price`
- `list-nutrition-foods` → **紧凑表格文本**（非 JSON）：
  ```text
  [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
    猪柳麦满分,null,1288,308,16,16,16,24,781,213
    板烧鸡腿堡,null,1638,391,23,17,35,1041,93
  ```
  `mcp_client.py` 内置了该文本格式的解析器，并按 `productName` 与菜单关联。

### 3.5 方案B：用 `query-meal-detail` 拆解套餐（已实现）

**实测结论（重要）：`query-meal-detail` 的返回里既没有营养字段，也没有过敏原字段。**
全量扫描其字段名只有：
`code / name / rounds / choices / modification / price / image / supportModify / diffPrice / quantity / maxQuantity / minQuantity / isDefault ...`

因此营养数据**仍然只能来自 `list-nutrition-foods`**。但 `query-meal-detail` 提供了一个关键能力：

| 能力 | 字段 | 用途 |
| --- | --- | --- |
| **套餐拆解** | `rounds[].choices[]`（取 `isDefault=1` 或 `quantity>0`） | 把套餐还原成子单品，例如「麦辣鸡腿汉堡三件套」→ 麦辣鸡腿汉堡 + 中薯条 + 可乐中杯 |
| **配料清单** | `modification.items[].values[]`（`selectedQuantity>0`） | 推断疑似过敏原，例如「吉士 / 高达干酪片 / 醇香芝士风味酱 / 黄油 / 奶油球」→ 疑似含乳 |

**为什么拆解套餐能解决「大量餐品热量为 0」**：
菜单里 115 款多是**套餐名**（如"麦辣鸡腿汉堡三件套"），而营养表里是**单品名**（如"麦辣鸡腿汉堡"），
两者对不上 → 热量为 0。拆解成子单品后按子单品名去匹配，命中率大幅提升：

| 方案 | 营养匹配率（实测上海某门店） |
| --- | --- |
| 基线：菜单名直接匹配营养表 | **19 / 115** |
| 方案B：套餐拆解后再匹配 | **82 / 115** |

匹配采用三级策略：精确名称 → 归一化（去括号/空格/™）→ **唯一包含匹配**（最长命中唯一才采纳，避免"薯条"误配多个规格）。

**性能**：单次 `query-meal-detail` 约 230ms，115 款餐品逐条调用约 **33 秒**，因此代码中内置了
`[进度] 已处理 x/115` 进度日志，并可通过配置项 `progress_every` 调整打印频率。

### 3.6 ⚠️ 关键限制：该 MCP 不提供「过敏原」权威数据

实测 35 个工具中**没有任何过敏原（allergen）相关工具**，
`list-nutrition-foods` 的字段仅有：
`productName / nutritionDescription / energyKj / energyKcal / protein / fat / carbohydrate / sodium / calcium`。

**影响**：本项目「过敏原筛查」这一核心卖点无法直接从 MCP 获取数据。
按设计的安全优先原则（数据缺失视为风险），所有餐品都会 `allergen_known=False` 并被过滤，
导致真实模式下推荐结果为空。

**可选处理方案**：

| 方案 | 说明 | 状态 |
| --- | --- | --- |
| A. 本地过敏原对照表 | 维护 `allergen-map.json`，按餐品名匹配过敏原 | 未实现（需人工维护数据） |
| B. 基于配料表推断 | 用 `query-meal-detail` 的配料清单推断风险 | ✅ **已实现（只做减法，仅供参考）** |
| C. 降级为「营养 + 价格」 | 弱化过敏原功能，如实说明数据限制 | 部分实现（strict 默认下即为此效果） |
| D. 未知当安全 | 放行未知餐品，但显式告警 | ✅ **已实现（opt-in）** |
| E. 名称守卫 | 餐品名直接含有过敏原名称时一律剔除 | ✅ **已实现（默认开启，只做减法）** |

### 已实现的三层策略

```text
第一层：名称守卫（任何策略下都生效，只做减法）
    餐品名含「牛奶/乳制品/奶昔/花生/鸡蛋…」等过敏原名称 → 直接剔除
    例：给牛奶过敏用户推荐「纯牛奶(盒装)」→ 被拦截（实测拦截 3 款）

第二层：配料推断（方案B，任何策略下都生效，只做减法）
    由 query-meal-detail 的配料清单推断疑似过敏原
    例：「吉士 / 高达干酪片 / 醇香芝士风味酱 / 黄油 / 奶油球 / 麦香鱼酱」
        → 疑似含乳 / 含鱼 → 命中用户黑名单则剔除（实测拦截 9 款）
    ⚠️ 配料清单是"可特调项"，并非完整配料表，因此推断结果必然不完整，
       只用于标记风险，绝不用于声明"不含过敏原"。

第三层：allergen_policy（处理"过敏原数据完全缺失"）
    strict（默认）—— 未知即风险，剔除该餐品（安全优先）
    warn        —— 未知当安全，放行但逐条显式告警（用于真实模式演示）
```

> 判定顺序即上表顺序：**名称守卫 → 配料推断命中 → 未知策略**。
> 第二层必须排在第三层之前，否则 warn 策略下带推断过敏原的餐品会被直接放行，失去保护作用。

配置方式（二选一）：

```bash
python src/main.py --allergen-policy warn          # 命令行
```
```json
{ "allergen_policy": "warn" }                      // mcp-config.json
```

> **为什么默认仍是 strict**：过敏原筛查是食品安全功能，
> 默认放宽会掩盖数据缺口。`warn` 是给演示与联调用的显式选择，
> 使用时会明确提示"无法保证不含过敏原，请向门店核实"。
>
> **两层兜底的边界**：名称守卫只识别"名字里写着过敏原"，配料推断只覆盖"可特调配料"。
> 像「圆筒冰淇淋」「阿芙佳朵」这类**名字不含过敏原名、配料清单里也没有乳制品字样**
> 但实际含乳的餐品，在真实模式（无权威过敏原数据）下仍会漏过——
> 这是关键词/配料方案的固有上限，也是为什么默认策略保持 strict。

---

## 四、密钥安全方案

### 1. 分层文件策略

| 文件 | 是否入库 | 内容 |
| --- | --- | --- |
| `mcp-config.example.json` | ✅ 提交 GitHub | 仅占位符，**不含任何真实 Token** |
| `mcp-config.json` | ❌ 已被 `.gitignore` 忽略 | 存放真实 Token、服务地址 |

### 2. 密钥读取优先级

程序按以下顺序读取密钥，避免密钥硬编码进代码：

1. 环境变量（CI / 服务器场景）：`MCD_MCP_TOKEN`、`MCD_MCP_URL`
2. 本地配置文件：`mcp-config.json`
3. 若均不存在 → 抛出友好提示，引导用户复制 `mcp-config.example.json`

### 3. 安全红线

- ❌ **严禁**把真实 Token 写入任何 `.py` 源码或文档
- ❌ **严禁**提交 `mcp-config.json` 到 Git
- ✅ 提交前必须执行 `git status` 检查暂存区
- ✅ 如不慎泄露，第一时间在 MCP 控制台**重置 Token**
- ✅ Token 仅在内存中使用，日志中需**脱敏打印**（只显示前 4 位）

### 4. .gitignore 关键规则

```gitignore
mcp-config.json
*.env
.venv/
__pycache__/
```

---

## 五、WorkBuddy 开发说明

本项目全程使用 **WorkBuddy** 作为开发工具（大赛专项奖励申报依据）。

- WorkBuddy 负责：项目脚手架生成、MCP 客户端与推荐引擎代码编写、文档撰写、开发日志记录
- 开发过程完整记录见 `docs/workbuddy-dev-log.md`
- 关键对话记录请粘贴到开发日志的"对话记录"章节，作为专项奖励核验材料

> 建议：每完成一个开发阶段，就在 `docs/workbuddy-dev-log.md` 中追加一条带时间戳的记录。

---

## 六、异常处理

`mcp_client.py` 对以下异常做统一捕获与友好降级：

| 异常类型 | 触发场景 | 处理策略 |
| --- | --- | --- |
| 配置文件缺失 | 无 `mcp-config.json` | 提示复制模板，退出 |
| Token 缺失 / 为空 | 未填写真实 Token | 友好报错并退出 |
| Token 含非 ASCII 字符 | 配置文件里仍留着中文占位符 | **前置拦截**，提示替换为真实 Token（避免难以理解的 `UnicodeEncodeError`） |
| 鉴权失败（HTTP 401/403） | Token 无效或已过期 | 通过 httpx 响应钩子捕获真实状态码（SDK 会把它吞成 JSON-RPC `-32603`），给出「检查 Token」提示 |
| HTTP 5xx | MCP 服务端异常 | 打印状态码与细节，返回空结果，不中断主流程 |
| 连接 / DNS 失败 | 网络不可达 | 提示检查网络连通性 |
| 超时 | 请求 MCP 超时 | 提示检查网络或代理 |
| **嵌套 ExceptionGroup** | asyncio TaskGroup 包装的异常 | **递归解包**取最底层真实异常，避免只看到 `unhandled errors in a TaskGroup` |
| 工具调用返回错误 | `isError=true` | 记录错误文本，返回 `None` |
| 返回体非 JSON | 工具返回文本（如营养表） | 尝试 JSON 解析；失败则走紧凑表格解析器 |
| 字段缺失 | 某餐品缺营养/过敏原字段 | 该字段按「未知」处理；过敏原未知时**默认标记为风险并过滤**（安全优先） |
| MCP SDK 未安装 | `import mcp` 失败 | 提示 `pip install mcp`；`--mock` 模式不受影响 |

### 安全优先原则

> 当过敏原数据**缺失或无法解析**时，推荐引擎采取**保守策略**：
> 将该餐品视为"可能含过敏原"并**剔除**，宁可少推荐，不可误推荐。

---

## 七、接口联调自测清单

- [x] `list_tools()` 能返回并打印全部工具名（实测 35 个）
- [x] `query-meals` 能返回非空餐品列表（实测 115 款）
- [x] `list-nutrition-foods` 紧凑表格能被正确解析（实测 158 条）
- [x] 菜单与营养表能按餐品名完成关联
- [ ] 过敏原数据获取（**该 MCP 无对应工具，待产品决策**）
- [x] 错误 Token 情况下输出 HTTP 401 友好提示，不崩溃
- [x] 日志中 Token 已脱敏（仅显示前 4 位）
- [x] 无 `mcp-config.json` 时给出复制模板指引
- [x] `--mock` 离线模式不受影响
