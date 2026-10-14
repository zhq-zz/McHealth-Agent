# -*- coding: utf-8 -*-
"""
main.py —— 麦麦饮食智能管家 · 命令行交互入口

流程：
    1. 接收用户自然语言需求（或命令行参数）
    2. 解析出：热量上限 / 预算上限 / 过敏原黑名单
    3. 调用 McpClient 拉取麦当劳餐品数据
    4. 调用 Recommender 完成过滤、筛选、汇总
    5. 打印结构化推荐结果

用法：
    python src/main.py                     # 交互式输入
    python src/main.py --mock              # 离线自测（无需 Token）
    python src/main.py --calories 500 --budget 30 --allergens 花生,牛奶
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import List

# 确保无论从哪个目录运行，都能 import 到同目录模块
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_client import McpClient, McpError                    # noqa: E402
from recommender import Recommender, UserProfile, ALLERGEN_ALIAS  # noqa: E402


# ---------------------------------------------------------------------------
# 自然语言需求解析
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 过敏原匹配模式预编译
# ---------------------------------------------------------------------------
# 说明：不再使用「裸子串匹配」，改为按词边界匹配，避免误判：
#   - 中文别名：要求 key 长度 >= 2（如"鸡蛋""牛奶"），天然不会命中"蛋糕""奶茶"
#   - 英文别名：加词边界断言，避免 "egg" 命中 "veggie"、"soy" 命中 "soybean" 之类的串扰
_ALLERGEN_PATTERNS: List[tuple] = []
for _alias in ALLERGEN_ALIAS:
    if _alias.isascii():
        # 英文：前后不能紧邻字母或数字
        _ALLERGEN_PATTERNS.append(
            (_alias, re.compile(rf"(?<![a-z0-9]){re.escape(_alias)}(?![a-z0-9])", re.IGNORECASE))
        )
    else:
        # 中文：直接整体匹配（别名本身已是完整词语）
        _ALLERGEN_PATTERNS.append((_alias, re.compile(re.escape(_alias))))


def parse_natural_language(text: str) -> UserProfile:
    """
    从中文自然语言中解析用户需求。

    支持示例：
        "我想吃一顿不超过 500 大卡的午餐，预算 30 元以内，我对花生和牛奶过敏"
    """
    text = text or ""

    # 1) 解析热量上限：匹配 "500大卡" / "不超过500 kcal" 等
    calories = 600.0
    cal_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:大卡|千卡|kcal|卡路里|卡)", text, re.IGNORECASE)
    if cal_match:
        calories = float(cal_match.group(1))

    # 2) 解析预算上限：匹配 "30元" / "预算30块" 等
    budget = 50.0
    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|rmb|￥)", text, re.IGNORECASE)
    if budget_match:
        budget = float(budget_match.group(1))

    # 3) 解析过敏原：按预编译的「词边界」模式匹配，避免单字/子串误判
    #    例："蛋糕"不会命中"鸡蛋"，"奶茶"不会命中"牛奶"，"veggie"不会命中"egg"
    allergens = set()
    for alias, pattern in _ALLERGEN_PATTERNS:
        if pattern.search(text):
            allergens.add(alias)

    # 4) 解析偏好分类（可选）
    prefer = ""
    for cat in ("汉堡", "小食", "饮料", "甜品", "沙拉"):
        if cat in text:
            prefer = cat
            break

    return UserProfile.from_allergen_names(
        list(allergens),
        max_calories=calories,
        max_budget=budget,
        prefer_category=prefer,
    )


# ---------------------------------------------------------------------------
# 结果打印
# ---------------------------------------------------------------------------

def print_result(profile: UserProfile, recommendations, removed, engine=None) -> None:
    """打印结构化推荐结果。"""
    print()
    print("=" * 52)
    print("          麦麦饮食智能管家 · 推荐结果")
    print("=" * 52)

    # 过敏原过滤概览
    if profile.allergen_blacklist:
        print(f"[过敏原过滤] 黑名单：{', '.join(sorted(profile.allergen_blacklist))}"
              f" | 已剔除 {len(removed)} 款风险餐品")
    else:
        print("[过敏原过滤] 未设置过敏原黑名单")
    # 过滤明细：名称守卫 / 配料推断 / 未知放行
    if engine is not None:
        print(f"[过滤明细] 名称守卫 {engine.name_guard_removed} 款"
              f" | 配料推断命中 {engine.inferred_removed} 款"
              f" | 未知放行 {engine.unknown_allowed} 款")
    print(f"[过敏原策略] {profile.allergen_policy}"
          + ("（未知即风险，严格过滤）" if profile.allergen_policy == "strict"
             else "（未知当安全，放行并告警）"))
    print(f"[筛选条件] 热量 ≤ {profile.max_calories:.0f} kcal | 预算 ≤ {profile.max_budget:.1f} 元")
    print()

    if not recommendations:
        # 区分「因过敏原数据缺失被全量过滤」和「真的没有可行组合」，避免给出错误建议
        if removed and all(not p.allergen_known for p in removed):
            print(f"没有可行组合的原因：{len(removed)} 款餐品全部因「过敏原数据不可用」被过滤。")
            print("放宽热量或预算上限不会有帮助，请改用：--allergen-policy warn")
        else:
            print("抱歉，在当前的过敏原 / 热量 / 预算约束下没有可行的餐品组合。")
            print("建议：适当放宽热量或预算上限，或减少过敏原限制。")
        print("=" * 52)
        return

    # 复用推荐引擎的报表渲染
    engine = Recommender(profile)
    for idx, rec in enumerate(recommendations, start=1):
        print(engine.render_report(rec, index=idx))
        print()

    print("=" * 52)
    print("提示：结果基于麦当劳 MCP 数据筛查，严重过敏请以官方配料表为准。")
    print("=" * 52)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_profile_from_args(args) -> UserProfile:
    """优先使用命令行参数；缺失时再进入交互式输入。"""
    if args.calories is not None or args.budget is not None or args.allergens:
        allergens = [a for a in re.split(r"[，,、\s]+", args.allergens or "") if a]
        return UserProfile.from_allergen_names(
            allergens,
            max_calories=args.calories if args.calories is not None else 600.0,
            max_budget=args.budget if args.budget is not None else 50.0,
        )

    # 交互式输入
    print("=" * 52)
    print("          麦麦饮食智能管家 McHealth Agent")
    print("=" * 52)
    print("请输入你的饮食需求，例如：")
    print("  我想吃一顿不超过 500 大卡的午餐，预算 30 元以内，我对花生和牛奶过敏")
    print("-" * 52)
    try:
        user_input = input("请输入你的饮食需求：").strip()
    except (EOFError, KeyboardInterrupt):
        user_input = ""
    if not user_input:
        print("未检测到输入，已使用默认需求（热量 ≤ 600 kcal | 预算 ≤ 50 元 | 无过敏原限制）。")
    return parse_natural_language(user_input)


def main() -> int:
    parser = argparse.ArgumentParser(description="麦麦饮食智能管家 · McHealth Agent（2026 麦当劳大赛参赛项目）")
    parser.add_argument("--calories", type=float, default=None, help="热量上限（kcal）")
    parser.add_argument("--budget", type=float, default=None, help="预算上限（元）")
    parser.add_argument("--allergens", type=str, default="", help="过敏原黑名单，逗号分隔，如：花生,牛奶")
    parser.add_argument("--mock", action="store_true", help="使用离线模拟数据自测（无需 Token）")
    parser.add_argument("--allergen-policy", choices=["strict", "warn"], default=None,
                        help="过敏原数据缺失时的策略：strict=剔除（默认，安全优先），"
                             "warn=放行但显式告警（用于真实模式演示）")
    parser.add_argument("--list-tools", action="store_true",
                        help="仅连接 MCP 并打印 tools/list 返回的全部工具，然后退出")
    parser.add_argument("--verbose", action="store_true", help="开启 DEBUG 日志")
    args = parser.parse_args()

    # 日志配置
    # 注意：--verbose 只放开「本项目」的 DEBUG 日志；
    # 底层网络库（httpx / httpcore / anyio / mcp）的 DEBUG 过于嘈杂，
    # 会把 tools/list 等业务输出淹没，因此统一压到 WARNING。
    # 同时列出 httpx2 / httpcore2 —— mcp 2.x 使用的是 httpx v2 系列（包名带 2 后缀）。
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    if args.verbose:
        logging.getLogger("mcp_client").setLevel(logging.DEBUG)
    for _noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "anyio", "mcp", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    # 命令行 --mock 时，通过环境变量传递给 McpClient
    if args.mock:
        os.environ["MCD_USE_MOCK"] = "1"

    # 1) 构造用户画像（--list-tools 只查工具列表，不需要画像，避免多余的交互输入）
    profile = UserProfile() if args.list_tools else build_profile_from_args(args)

    # 2) 拉取餐品数据
    try:
        client = McpClient(use_mock=args.mock)
    except McpError as exc:
        print(f"\n[配置错误] {exc}")
        print("提示：可先用 `python src/main.py --mock` 在离线模式下体验完整流程。")
        return 2

    # 2.0) 确定过敏原策略：命令行参数 > 配置文件 > 默认 strict（安全优先）
    if not args.list_tools:
        policy = (args.allergen_policy
                  or client.config.get("allergen_policy")
                  or "strict")
        profile.allergen_policy = policy

    # 2.1) 真实连接时，先调用 tools/list 并打印麦当劳 MCP 提供的全部工具
    if not args.mock:
        print(f"\n正在连接麦当劳 MCP（{client.config.get('mcp_server_url')}）并调用 tools/list ...")
        try:
            client.list_tools(print_result=True)
        except McpError as exc:
            print(f"\n[MCP 连接失败] {exc}")
            print("提示：可先用 `python src/main.py --mock` 在离线模式下体验完整流程。")
            return 4
        if args.list_tools:
            print("\n（--list-tools 模式，仅列出工具后退出）")
            return 0

    print("\n正在从麦当劳 MCP 获取餐品数据 ...")
    try:
        products = client.get_all_products()
    except McpError as exc:
        print(f"\n[MCP 调用失败] {exc}")
        print("提示：可先用 `python src/main.py --mock` 在离线模式下体验完整流程。")
        return 4
    if not products:
        print("[错误] 未能获取到餐品数据，请检查网络、Token 或 MCP 服务状态。")
        print("提示：可先用 `python src/main.py --mock` 在离线模式下体验完整流程。")
        return 3
    print(f"已获取 {len(products)} 款餐品。")

    # 2.1.5) 输出取数统计（处理总数 / 营养命中 / 过敏原来源）
    stats = client.stats or {}
    if stats:
        print("-" * 52)
        print("[取数统计]")
        print(f"  共处理餐品：{stats.get('total', 0)} 款"
              f"（套餐 {stats.get('combo', 0)} / 单品 {stats.get('single', 0)}）")
        print(f"  营养表条目：{stats.get('nutrition_table_size', 0)} 条")
        print(f"  成功拿到有效营养数据：{stats.get('nutrition_ok', 0)} 款")
        print(f"  过敏原·权威数据（MCP 提供）：{stats.get('allergen_authoritative', 0)} 款")
        print(f"  过敏原·配料推断（仅供参考）：{stats.get('allergen_inferred', 0)} 款")
        if stats.get("detail_fail"):
            print(f"  详情获取失败：{stats.get('detail_fail')} 款")
        print("-" * 52)

    # 2.2) 重要提示：若 MCP 未提供过敏原数据，全部餐品的过敏原都是「未知」。
    #      strict 策略下会被全部过滤（结果为空）；warn 策略下会放行但显式告警。
    if products and all(not p.allergen_known for p in products):
        print()
        print("!" * 52)
        print("⚠️  过敏原数据提示")
        print("  麦当劳 MCP 未提供「过敏原」权威数据源，全部餐品的 allergen_known 均为 False。")
        inferred = sum(1 for p in products if p.allergen_source == "ingredient_inferred")
        print(f"  已用 query-meal-detail 的配料清单推断出 {inferred} 款餐品的疑似过敏原（仅供参考）。")
        if profile.allergen_policy == "warn":
            print("  当前策略：warn —— 未知当安全，餐品会被放行，但结果中会显式标注风险。")
            print("  ⚠️ 该模式下无法保证餐品不含你的过敏原，请务必向门店核实。")
        else:
            print("  当前策略：strict —— 未知即风险，所有餐品都会被过滤，推荐结果为空。")
            print("  如需在真实模式下查看推荐效果，可加参数：--allergen-policy warn")
        print("!" * 52)
        print()
        print("  其他可选方式：")
        print("    1) 使用 --mock 模式演示完整推荐流程（内置含过敏原的模拟数据）")
        print("    2) 在 mcp-config.json 中配置 allergen_policy 持久化该策略")
        print("!" * 52)

    # 3) 推荐
    engine = Recommender(profile)
    recommendations, removed = engine.recommend(products, top_n=3)

    # 4) 打印结果
    print_result(profile, recommendations, removed, engine=engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
