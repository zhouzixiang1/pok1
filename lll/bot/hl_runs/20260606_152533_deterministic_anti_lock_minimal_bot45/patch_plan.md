# 补丁草案

本文件只给出人工复盘建议，不会自动修改 `1.py`。

## 优先级

- 候选表现较好。合入任何策略修改前，优先补充对应回放/回归用例。

## 建议关注点

- 复盘提取出的 anti-lock 手牌，检查 `fold_gives_opponent_lock` 门槛和 `choose_anti_lock_pressure_action` 尺寸。
- 检查连续 check 后的小探测手牌；只有当亏损集中在这里时，才收紧湿润牌面或弃牌率条件。
- 从 `interesting_hands.jsonl` 中最高亏损手牌开始，区分亏损来自翻前防守、翻后打光，还是错过价值。

## 回归检查清单

- `sanitize_action` 能保证所有返回动作合法。
- 普通非 anti-lock 场景下，垃圾牌和非 3bet 候选仍保持保守纪律。
- 当弃牌会让对手进入锁定时，anti-lock 仍作为有边界的例外保留。
- 连续 check 后的 `100` 小探测/偷池路径仍然可用。
- 回放用例使用完整 Botzone 请求 payload。
