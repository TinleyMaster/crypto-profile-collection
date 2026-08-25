"""测试 classifier 是否能正常工作"""
import os
import sys

WORKSPACE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(WORKSPACE, "src"))
sys.path.insert(0, os.path.join(WORKSPACE, "..", "workbench", "kol"))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto",
)

from classifier import classify_post

# 测试一个明显的 prediction
test1 = "BTC 回踩 63000 接多，止损 62000，止盈 68000"
print("Test 1 (prediction):")
r = classify_post(test1)
print(r)
print()

# 测试一个明显的 noise
test2 = "今天天气真好，出去吃了顿火锅，太开心了！"
print("Test 2 (noise):")
r = classify_post(test2)
print(r)
print()

# 测试支撑位/压力位
test3 = "ETH 当前压力位 2476-2496，支撑位 2400，建议观望"
print("Test 3 (analysis with levels):")
r = classify_post(test3)
print(r)
