# workbuddy.md · McHealth Agent 的 WorkBuddy 开发记录

> 本文件用于 **2026 麦当劳程序员创意开发大赛「WorkBuddy 专项奖励」核验**。
> 内容为使用 WorkBuddy 开发本项目的对话上下文与阶段记录（已脱敏，不含任何真实 Token / 密钥 / 个人隐私）。

---

## 一、开发基本信息

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 麦麦饮食智能管家（McHealth Agent） |
| 开发工具 | WorkBuddy |
| 开发模式 | AI 辅助开发（需求 → 代码 → 文档 → 测试 → 发布 全流程） |
| 开发周期 | 2026-10-12 至 2026-10-14 |
| 项目仓库 | https://github.com/zhq-zz/McHealth-Agent |

---

## 二、使用 WorkBuddy 完成的开发阶段

### 阶段 1：需求分析与选题

- **目标**：确定差异化选题
- **产出**：确定「营养配餐 + 过敏原筛查」方向，面向减脂 / 控糖 / 食物过敏人群
- **WorkBuddy 使用**：需求梳理、竞品差异化分析、技术方案选型

### 阶段 2：项目脚手架与文件结构

- **目标**：搭建项目目录与基础文件
- **产出**：README.md、MCP_INTEGRATION.md、CONTEST_DECLARATION.md、mcp-config.example.json、.gitignore、requirements.txt、src/ 三个源码文件、docs/ 开发日志
- **WorkBuddy 使用**：一次性生成目录结构与全部文件内容并落地到本地工作空间

### 阶段 3：MCP 客户端开发

- **目标**：封装麦当劳 MCP 接口
- **产出**：src/mcp_client.py
- **WorkBuddy 使用**：接口封装、异常处理、Token 脱敏、字段兼容归一化
- **关键发现**：麦当劳 MCP 端点 https://mcp.mcd.cn 使用标准 **Streamable HTTP** 协议（非 REST），
  `tools/list` 实测返回 **35 个工具**；鉴权为请求头 `Authorization: Bearer {token}`

### 阶段 4：推荐引擎开发

- **目标**：实现过敏原过滤 + 热量预算筛选 + 营养汇总
- **产出**：src/recommender.py
- **WorkBuddy 使用**：算法设计、数据结构定义、报表渲染
- **关键设计**：三层过敏原策略 —— 餐品名关键词守卫 → 配料清单推断 → 未知项策略（strict / warn）；
  strict 为默认（食品安全功能不应默认放宽）

### 阶段 5：命令行入口与全链路联调

- **目标**：打通「自然语言 → 数据获取 → 推荐 → 打印」全流程
- **产出**：src/main.py
- **WorkBuddy 使用**：自然语言解析、CLI 参数设计、结构化输出、日志分级
- **关键成果**：方案B —— 遍历 `query-meals` 全量餐品并调用 `query-meal-detail` 拆解套餐后，
  营养命中率由 **19/115 提升至 82/115**

### 阶段 6：文档完善、安全自检与仓库发布

- **目标**：完善文档、校验密钥安全、发布仓库
- **产出**：README.md、MCP_INTEGRATION.md、CONTEST_DECLARATION.md、LICENSE、本文件
- **WorkBuddy 使用**：文档撰写、结构梳理、自检清单生成、Git 初始化与推送

---

## 三、与 WorkBuddy 的关键对话记录

```text
1. 项目选题阶段：用户计划参加麦当劳MCP大赛，需要差异化参赛项目。最终选定「麦当劳MCP健康饮食智能管家 McHealth-Agent」项目，项目目标：对接麦当劳MCP Streamable HTTP接口，根据用户热量上限、食物过敏原禁忌，自动筛选符合条件的麦当劳餐品，区分套餐并拆解单品，提供个性化健康点餐推荐。
2. 项目文档搭建：依次完成项目完整方案撰写，生成参赛仓库核心文档：README.md、MCP_INTEGRATION.md、CONTEST_DECLARATION.md参赛声明、需求功能清单。
3. 项目文件与代码开发：
- 搭建项目目录结构，生成基础代码骨架；
- 对接麦当劳MCP官方接入文档，原代码使用普通REST请求，发现协议错误，重写src/mcp_client.py，使用mcp官方Python SDK streamable_http_client，鉴权采用Bearer Token；
- 保留离线mock测试模式，支持--mock参数，不依赖MCP接口也可以本地验证逻辑；
- 业务逻辑开发：支持读取麦当劳菜单列表、营养数据、价格信息；处理套餐拆解逻辑；设计两套过敏原过滤策略：strict严格模式（过敏原未知餐品直接过滤）、warn告警模式（过敏原未知餐品放行，增加提示），解决套餐单品过敏原数据缺口问题；
- 完善异常捕获、日志打印，测试用例：「我对牛奶过敏，推荐350大卡的午餐」，完成全链路功能测试。
4. 安全校验：反复检查项目文件，防止MCP真实Token泄露；本地mcp-config.json加入.gitignore，不会上传GitHub；公开示例配置文件mcp-config.example.json仅保留Token占位符，无真实密钥。
5. 仓库与提交准备：收集参赛个人信息（姓名：张皓乔，个人参赛，GitHub用户名zhq-zz，联系电话、邮箱），自动填充参赛声明文档；新建GitHub公开仓库McHealth-Agent，初始化Git仓库，推送全部项目代码；补全参赛声明中的GitHub仓库地址。
6. 剩余待完成：补齐WorkBuddy开发日志关键对话记录，最后一次git推送，完成全部参赛材料准备，准备提交官方Issue报名参赛。
```

---

## 四、WorkBuddy 使用亮点

1. **协议纠错**：WorkBuddy 通过实机验证发现「麦当劳 MCP 是 Streamable HTTP 而非 REST」，
   纠正了初版基于 REST 的错误实现，避免项目从根上跑不通。
2. **依赖环境排障**：定位到 `mcp` 2.x 的 API 变化（`streamablehttp_client` → `streamable_http_client`、
   `FastMCP` → `MCPServer`），并发现底层 HTTP 库是 `httpx2` 而非 `httpx`，
   改为**版本无关的兼容导入**，而非盲目按报错改名。
3. **数据链路重构**：通过套餐拆解把营养命中率从 19/115 提升到 82/115。
4. **安全红线把关**：多轮校验 `.gitignore` 与暂存区，确保真实 Token 从未进入 Git 历史。
5. **全文档流水线**：从需求、代码到参赛文档与提交材料，全流程由 WorkBuddy 完成。

---

## 五、脱敏说明

本文件及本仓库全部提交内容均**不包含**真实 Token、密钥、密码、账号凭证，
亦不包含他人个人信息；`mcp-config.example.json` 仅使用环境变量 / 占位符形式。
