# -*- coding: utf-8 -*-
"""
recommender.py —— 麦麦饮食智能管家推荐引擎

核心职责（MVP 三件事）：
    1. 过敏原过滤：按用户过敏原黑名单剔除风险餐品（安全优先）
    2. 热量 / 预算筛选：在双重上限约束下挑选可行组合
    3. 营养汇总计算：输出热量 / 蛋白质 / 脂肪 / 碳水 / 钠 与价格统计

设计原则：
    - 安全第一：过敏原数据缺失的餐品一律视为风险并剔除
    - 无外部依赖：本模块只消费 Product 数据，不直接访问网络
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from mcp_client import Product

# ---------------------------------------------------------------------------
# 过敏原中英文别名映射
# 用户可能输入中文（如"花生"），MCP 返回英文 token（如 "peanuts"），此处做归一
# ---------------------------------------------------------------------------
ALLERGEN_ALIAS: Dict[str, str] = {
    "花生": "peanuts",
    "peanut": "peanuts",
    "peanuts": "peanuts",
    "牛奶": "milk",
    "乳制品": "milk",
    "milk": "milk",
    "dairy": "milk",
    "鸡蛋": "egg",
    "egg": "egg",
    "eggs": "egg",
    "麸质": "gluten",
    "小麦": "gluten",
    "面筋": "gluten",
    "gluten": "gluten",
    "wheat": "gluten",
    "大豆": "soy",
    "黄豆": "soy",
    "soy": "soy",
    "soybean": "soy",
    "坚果": "nuts",
    "nuts": "nuts",
    "tree_nuts": "nuts",
    "芝麻": "sesame",
    "sesame": "sesame",
    "海鲜": "shellfish",
    "贝类": "shellfish",
    "shellfish": "shellfish",
    "鱼类": "fish",
    "fish": "fish",
}

# 注意：别名表中「禁止」保留单字 key（如"蛋""奶""鱼"）。
# 原因：main.py 采用文本关键词匹配，单字 key 会误伤同音/同字的正常词汇，
#       例如"蛋糕"会被误判为鸡蛋过敏、"奶茶"会被误判为牛奶过敏。
# 约束：所有 key 长度必须 >= 2 个字符（英文单词除外，英文按词边界匹配）。


def normalize_allergen(name: str) -> str:
    """把用户输入的过敏原统一归一化为标准 token（小写）。"""
    key = str(name).strip().lower()
    return ALLERGEN_ALIAS.get(key, key)


# ---------------------------------------------------------------------------
# 名称守卫关键词（保守兜底，只做减法）
# ---------------------------------------------------------------------------
# 背景：麦当劳 MCP 未提供过敏原数据（详见 MCP_INTEGRATION.md 第三节第 4 条），
# 因此「过敏原未知」的餐品在 warn 策略下会被放行——但若餐品名本身就写着过敏原名称
# （例如给牛奶过敏用户推荐「纯牛奶」），那显然是必须拦截的。
# 这里仅收录「本身就是过敏原名称」的完整关键词，只用于排除，绝不用于放行，
# 因此该规则只会让推荐更保守、更安全，不会引入新风险。
# 约定：关键词长度 >= 2，与项目「禁止单字过敏原 key」的约定保持一致。
NAME_GUARD_KEYWORDS: Dict[str, tuple] = {
    "milk": ("牛奶", "乳制品", "奶昔"),
    "peanuts": ("花生",),
    "egg": ("鸡蛋",),
    "gluten": ("麸质", "小麦", "面筋"),
    "soy": ("大豆", "黄豆", "豆浆"),
    "nuts": ("坚果",),
    "sesame": ("芝麻",),
    "fish": ("鱼类",),
    "shellfish": ("海鲜", "贝类"),
}


# ---------------------------------------------------------------------------
# 用户画像
# ---------------------------------------------------------------------------

@dataclass
class UserProfile:
    """用户需求画像。"""

    max_calories: float = 600.0                 # 单餐热量上限（kcal）
    max_budget: float = 50.0                    # 预算上限（元）
    allergen_blacklist: Set[str] = field(default_factory=set)  # 过敏原黑名单（标准 token）
    prefer_category: str = ""                   # 可选：偏好分类（如"汉堡"）

    # 过敏原数据缺失时的处理策略（麦当劳 MCP 未提供过敏原数据，见 MCP_INTEGRATION.md）：
    #   "strict" —— 未知即风险，直接剔除该餐品（默认，安全优先）
    #   "warn"   —— 未知当安全，放行该餐品，但在结果中显式标注"过敏原未知，请自行确认"
    allergen_policy: str = "strict"

    @classmethod
    def from_allergen_names(cls, names: List[str], **kwargs) -> "UserProfile":
        """由用户输入的中文/英文过敏原名称构造画像。"""
        return cls(allergen_blacklist={normalize_allergen(n) for n in names if n}, **kwargs)


# ---------------------------------------------------------------------------
# 推荐结果
# ---------------------------------------------------------------------------

@dataclass
class Recommendation:
    """一次完整的推荐结果。"""

    items: List[Product] = field(default_factory=list)   # 推荐餐品组合
    total_calories: float = 0.0
    total_protein: float = 0.0
    total_fat: float = 0.0
    total_carbs: float = 0.0
    total_sodium: float = 0.0
    total_price: float = 0.0
    warnings: List[str] = field(default_factory=list)     # 过敏 / 营养预警

    @property
    def remaining_budget(self) -> float:
        """由外部传入预算时计算剩余预算（此处仅占位，实际在 Recommender 中计算）。"""
        return self.total_price


# ---------------------------------------------------------------------------
# 推荐引擎
# ---------------------------------------------------------------------------

class Recommender:
    """推荐引擎主体。"""

    # 组合中希望包含的餐品角色（主餐 / 配餐 / 饮料），用于搭建均衡搭配
    ROLE_MAIN = ("汉堡", "主食", "堡", "卷")
    ROLE_SIDE = ("小食", "配餐", "沙拉", "玉米")
    ROLE_DRINK = ("饮料", "饮", "茶", "咖啡")

    def __init__(self, profile: UserProfile):
        self.profile = profile
        self.unknown_allowed = 0     # 过敏原未知但被放行的餐品数（warn 策略下统计）
        self.name_guard_removed = 0  # 被名称守卫拦下的餐品数
        self.inferred_removed = 0    # 因「配料推断过敏原」被拦下的餐品数

    # ---------------- 第一步：过敏原过滤 ----------------

    def _name_guard_hit(self, product: Product) -> Optional[str]:
        """名称守卫：判断餐品名是否直接含有用户过敏原的名称。

        :return: 命中的过敏原 token，未命中返回 None
        """
        name = product.name or ""
        for allergen in self.profile.allergen_blacklist:
            for keyword in NAME_GUARD_KEYWORDS.get(allergen, ()):
                if keyword in name:
                    return allergen
        return None

    def filter_by_allergens(self, products: List[Product]) -> Tuple[List[Product], List[Product]]:
        """
        按过敏原黑名单过滤餐品。

        对「过敏原数据缺失」的餐品，按 profile.allergen_policy 处理：
            strict —— 视为风险，剔除（默认）
            warn   —— 放行，但会在结果中显式标注风险
        无论哪种策略，都会先执行「名称守卫」：
            若餐品名直接含有过敏原名称（如"纯牛奶"对牛奶过敏者），一律剔除。

        :return: (安全餐品列表, 被剔除的餐品列表)
        """
        safe: List[Product] = []
        removed: List[Product] = []
        self.unknown_allowed = 0   # 统计因 warn 策略而放行的「过敏原未知」餐品数
        self.name_guard_removed = 0  # 统计被名称守卫拦下的餐品数

        for p in products:
            # 第一层：名称守卫（只做减法，任何策略下都生效）
            if self._name_guard_hit(p):
                self.name_guard_removed += 1
                removed.append(p)
                continue

            # 第二层：黑名单命中（含由配料推断出的疑似过敏原）→ 一律剔除。
            #   注意：该检查必须在「数据缺失」分支之前，否则 warn 策略下
            #   带推断过敏原的餐品会被直接放行，失去保护作用。
            if self.profile.allergen_blacklist & p.allergens:
                if p.allergen_source == "ingredient_inferred":
                    self.inferred_removed += 1
                removed.append(p)
                continue

            # 第三层：过敏原数据缺失（非权威）→ 按策略处理
            if not p.allergen_known:
                if self.profile.allergen_policy == "warn":
                    self.unknown_allowed += 1
                    safe.append(p)
                else:
                    removed.append(p)
                continue

            safe.append(p)

        return safe, removed

    # ---------------- 第二步：热量 / 预算筛选 ----------------

    def filter_by_constraints(self, products: List[Product]) -> List[Product]:
        """在热量上限与预算上限内筛选可行餐品。"""
        return [
            p for p in products
            if p.calories <= self.profile.max_calories and p.price <= self.profile.max_budget
        ]

    # ---------------- 第三步：组合推荐 ----------------

    @staticmethod
    def _match_role(product: Product, roles: tuple) -> bool:
        """判断餐品是否属于某个角色（宽松匹配分类名）。"""
        text = f"{product.category}{product.name}"
        return any(role in text for role in roles)

    def recommend(self, products: List[Product], top_n: int = 3) -> Tuple[List[Recommendation], List[Product]]:
        """
        生成推荐结果。

        :param products: 原始餐品列表（来自 McpClient.get_all_products）
        :param top_n: 返回的推荐组合数量
        :return: (推荐组合列表, 被过敏原剔除的餐品列表)
        """
        # 1. 过敏原过滤
        safe, removed = self.filter_by_allergens(products)
        # 2. 热量 / 预算筛选
        feasible = self.filter_by_constraints(safe)

        # 3. 按"热量效率"排序（单位价格的营养产出，仅为排序启发式）
        feasible.sort(key=lambda p: (-p.protein, p.calories))

        recommendations: List[Recommendation] = []
        mains = [p for p in feasible if self._match_role(p, self.ROLE_MAIN)]
        sides = [p for p in feasible if self._match_role(p, self.ROLE_SIDE)]
        drinks = [p for p in feasible if self._match_role(p, self.ROLE_DRINK)]

        # 以主餐为锚点，围绕不同主餐构造 top_n 个组合
        for main in mains[:top_n]:
            combo: List[Product] = [main]
            used_price = main.price
            used_cal = main.calories

            # 尝试补充一份配餐
            for side in sides:
                if side in combo:
                    continue
                if used_price + side.price <= self.profile.max_budget and \
                        used_cal + side.calories <= self.profile.max_calories:
                    combo.append(side)
                    used_price += side.price
                    used_cal += side.calories
                    break

            # 尝试补充一份饮料
            for drink in drinks:
                if drink in combo:
                    continue
                if used_price + drink.price <= self.profile.max_budget and \
                        used_cal + drink.calories <= self.profile.max_calories:
                    combo.append(drink)
                    used_price += drink.price
                    used_cal += drink.calories
                    break

            recommendations.append(self._summarize(combo))

        # 若没有任何主餐命中也无组合，则退化：直接返回热量最低的若干单品组合
        if not recommendations and feasible:
            lowest = sorted(feasible, key=lambda p: p.calories)[:1]
            recommendations.append(self._summarize(lowest))

        return recommendations, removed

    # ---------------- 营养 / 价格汇总 ----------------

    def _summarize(self, items: List[Product]) -> Recommendation:
        """对一组餐品做营养与价格汇总，并生成预警提示。"""
        rec = Recommendation(items=list(items))
        for p in items:
            rec.total_calories += p.calories
            rec.total_protein += p.protein
            rec.total_fat += p.fat
            rec.total_carbs += p.carbs
            rec.total_sodium += p.sodium
            rec.total_price += p.price

        # 营养评估预警
        if rec.total_calories > self.profile.max_calories:
            rec.warnings.append(f"热量 {rec.total_calories:.0f} kcal 已超上限 {self.profile.max_calories:.0f} kcal")
        if rec.total_price > self.profile.max_budget:
            rec.warnings.append(f"总价 {rec.total_price:.1f} 元 已超预算 {self.profile.max_budget:.1f} 元")
        if rec.total_sodium > 2000:
            rec.warnings.append(f"钠含量偏高（{rec.total_sodium:.0f} mg），建议注意当日整体摄入")

        # 过敏原二次校验（防御性检查）
        hit = set()
        for p in items:
            hit |= (p.allergens & self.profile.allergen_blacklist)
        if hit:
            rec.warnings.append(f"⚠️ 检测到过敏原：{', '.join(sorted(hit))}，请勿食用")

        # 过敏原数据缺失提示：warn 策略下放行的餐品必须显式告知用户
        unknown_in_combo = [p for p in items if not p.allergen_known]
        if unknown_in_combo:
            names = "、".join(p.name for p in unknown_in_combo)
            rec.warnings.append(
                f"⚠️ 过敏原数据不可用：{names} 未获取到过敏原信息，"
                f"无法确认是否含有你的过敏原，请务必向门店核实"
            )

        return rec

    # ---------------- 报表渲染（供 main.py 复用） ----------------

    def render_report(self, rec: Recommendation, index: int = 1) -> str:
        """把单个推荐结果渲染为结构化文本报表。"""
        lines: List[str] = []
        lines.append(f"【推荐组合 {index}】")
        for p in rec.items:
            lines.append(
                f"  - {p.name:<12} 热量 {p.calories:>5.0f} kcal   价格 {p.price:>5.1f} 元"
            )
        lines.append("  " + "-" * 48)
        lines.append(
            f"  合计：热量 {rec.total_calories:.0f} kcal | 蛋白质 {rec.total_protein:.1f}g | "
            f"脂肪 {rec.total_fat:.1f}g | 碳水 {rec.total_carbs:.1f}g | 钠 {rec.total_sodium:.0f}mg"
        )
        lines.append(
            f"  合计：价格 {rec.total_price:.1f} 元 | 剩余预算 {self.profile.max_budget - rec.total_price:.1f} 元"
        )
        # 营养评估
        if rec.total_calories <= self.profile.max_calories:
            lines.append("  [营养评估] 热量符合上限约束 ✅")

        # 过敏相关预警与「其他提示」分开渲染，避免同一条警告被打印两次
        allergen_warnings = [w for w in rec.warnings if "过敏原" in w]
        other_warnings = [w for w in rec.warnings if "过敏原" not in w]

        if allergen_warnings:
            for w in allergen_warnings:
                lines.append(f"  [过敏预警] {w}")
        else:
            lines.append("  [过敏预警] 本次推荐组合未检测到你的过敏原 ✅")

        for w in other_warnings:
            lines.append(f"  [提示] {w}")
        return "\n".join(lines)
