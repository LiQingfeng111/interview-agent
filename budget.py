"""预算与降级：让"这一场花了多少钱"变成一个能打在报告上的数字。

为什么现在做：
  免费中继不稳定，用户也不知道一场面试烧多少 token。
  没有预算概念，模型一旦抽风（每轮 3k token）你就会在账单/超时上才发现。

三级降级（超限自动降，且必须留痕）：
  level 0 正常
  level 1 收紧：关掉事实抽取、缩短参考资料（省 token，但仍用模型）
  level 2 只走兜底：不再调用模型，问题/评分全部走确定性路径

价格表内置 15 行就够（借 genai-prices 的思路，不装包）。
"""
from __future__ import annotations

import dataclasses

# 分/千 token（输入, 输出）。未知模型走 DEFAULT。
PRICE_TABLE = {
    "lobsterai/deepseek-flash": (0.05, 0.15),
    "deepseek-chat": (0.1, 0.28),
    "deepseek-reasoner": (0.14, 0.55),
    "gpt-4o-mini": (1.1, 4.4),
    "qwen-plus": (0.08, 0.2),
}
DEFAULT_PRICE = (0.1, 0.3)


def price_of(model: str) -> tuple[float, float, bool]:
    """返回 (输入价, 输出价, 是否估算)。名字里带匹配即可，便于带版本后缀。

    单位是**分/千 token**（和国内厂商报价口径一致）。
    转成人民币要再除 100——第一版忘了这一步，一场 10k token 的 mock 面试
    被算成 ¥1.09，直接触发了"预算用尽"提前收尾。
    """
    if not model:
        return (*DEFAULT_PRICE, True)
    low = model.lower()
    if low.startswith("mock") or low.startswith("replay"):
        return (0.0, 0.0, True)
    for key, price in PRICE_TABLE.items():
        if key.lower() in low:
            return (*price, False)
    for key, price in PRICE_TABLE.items():
        stem = key.split("/")[-1].split("-")[0]
        if stem and stem in low:
            return (*price, True)
    return (*DEFAULT_PRICE, True)


def estimate_tokens(text: str) -> int:
    """无 usage 时的估算：中文按 1 字≈1 token，英文按 4 字符≈1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


@dataclasses.dataclass
class Budget:
    tokens: int = 60_000
    cost_cny: float = 1.0
    seconds: int = 600

    used_tokens: int = 0
    used_cost: float = 0.0
    started: float = 0.0
    exceeded_log: list[str] = dataclasses.field(default_factory=list)

    def spend(self, prompt: str = "", completion: str = "", model: str = "",
              tokens_in: int = 0, tokens_out: int = 0) -> dict:
        """记一次调用。返回本次明细，便于写事件。"""
        if not tokens_in:
            tokens_in = estimate_tokens(prompt)
        if not tokens_out:
            tokens_out = estimate_tokens(completion)
        pin, pout, estimated = price_of(model)
        # 分/千 token → 元：先按千 token 算分，再除 100 换成元
        cost = ((tokens_in / 1000) * pin + (tokens_out / 1000) * pout) / 100
        self.used_tokens += tokens_in + tokens_out
        self.used_cost = round(self.used_cost + cost, 6)
        return {
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_cny": round(cost, 6), "estimated": estimated,
        }

    def level(self) -> int:
        """0 正常 / 1 收紧 / 2 只走兜底。"""
        if self.used_tokens >= self.tokens or self.used_cost >= self.cost_cny:
            return 2
        if self.used_tokens >= self.tokens * 0.75 or self.used_cost >= self.cost_cny * 0.75:
            return 1
        return 0

    def note(self, why: str) -> None:
        if why not in self.exceeded_log:
            self.exceeded_log.append(why)

    def summary(self) -> dict:
        return {
            "tokens": self.used_tokens,
            "budget_tokens": self.tokens,
            "cost_cny": round(self.used_cost, 4),
            "budget_cost_cny": self.cost_cny,
            "level": self.level(),
            "degraded": list(self.exceeded_log),
        }

    def render(self) -> str:
        lv = self.level()
        tag = {0: "正常", 1: "收紧（关闭事实抽取）", 2: "兜底（不调模型）"}[lv]
        return (f"成本：{self.used_tokens}/{self.tokens} token · "
                f"¥{self.used_cost:.4f}/{self.cost_cny:.2f} · 状态 {tag}")
