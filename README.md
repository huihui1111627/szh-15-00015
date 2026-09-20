# 盾构施工剖面控制系统

把刀盘扭矩、推进速度、土仓压力、注浆量与地层变化组合成**可操作的施工剖面**，
支持沿线路选择预测区段、调整下一阶段掘进参数，并随推进过程更新地表沉降、
刀具磨损与涌水风险；地层突变或多参数共同越界时保留异常前后状态并给出连锁原因。
支持从历史稳定节点克隆多方案并行推进，并处理传感数据迟到、人工指令与自动保护
冲突、服务重启状态接续。

## 设计要点

- **事件溯源**：读数、人工调参、保护确认、预测区段、方案克隆全部追加到 JSONL
  事件日志（`store.py`）；当前状态只是事件流的确定性投影（`engine.py` 的 `fold`），
  服务重启后重放日志即可无缝接续，无需额外快照即可恢复。
- **可操作剖面**：每类地层（黏土/粉土/砂层/砂砾）有默认参数带 `Band`
  （扭矩、速度、土仓压力、注浆量上下限），人工调参按地层保存并即时生效。
- **随环更新的风险**：环闭环时由 `rules.ring_risks` 结算
  - 地表沉降：土仓欠压占主导，叠加快推进与注浆不足；
  - 刀具磨损：与地层磨蚀性、平均扭矩正相关，与推进速度负相关；
  - 涌水风险：地层渗透系数 × 水头，欠压时自动上调一级。
- **异常留痕**：地层突变单独成案；两项及以上参数共同越界按环归并为一条异常，
  保留异常前/后状态快照和连锁原因链（欠压→沉降、注浆不足→固结沉降、
  超载→刀具磨损→自动保护联锁）。
- **自动保护与仲裁**：`joint_limit_trip`、`water_inrush_guard`、
  `stratum_change_guard`、`cutter_overload_trip` 触发后锁定，需连续 3 条安全
  读数 + 人工确认才解除；保护生效期间人工指令默认抑制并审计，也可 `force` 强制覆盖。
- **迟到数据**：以单调工程时钟判定，30 分钟窗口内的迟到读数按事件时间重放，
  修订受影响的环评估与异常（标记 `amended`）；超窗口读数只入档不参与计算。
- **多方案并行**：只能从稳定节点（闭环无异常/保护、越界率 <20%）克隆，
  克隆只复制该节点前的原始事件，派生审计由重放重新生成；克隆后各方案独立推进、
  独立调参、互不影响。

## 目录结构

- `tunnel_profile/models.py` — 地层、区段、参数带、读数、异常、保护、环统计
- `tunnel_profile/rules.py` — 越界判定、风险模型、保护规则、连锁原因链
- `tunnel_profile/store.py` — 仅追加 JSONL 事件日志
- `tunnel_profile/engine.py` — 事件溯源投影、调参仲裁、迟到重放、稳定节点克隆
- `tunnel_profile/simulator.py` — 确定性线路推进仿真与故障注入
- `tunnel_profile/report.py` — 方案状态文字剖面
- `demo.py` — 九段端到端演示
- `tests/test_system.py` — 14 个能力测试（标准库 unittest）

## 运行

```bash
python3 demo.py                 # 演示（自动生成临时事件日志）
python3 demo.py tunnel.jsonl    # 演示并保留事件日志，可重启续算
python3 -m unittest discover -s tests -v
```

## 最小用法

```python
from tunnel_profile import Engine, EventStore, Reading, Segment

eng = Engine(EventStore("tunnel.jsonl"))
eng.create_scenario("主方案", [
    Segment("S1", 0.0, 50.0, "clay"),
    Segment("S2", 50.0, 100.0, "sand"),
], water_heads={"S2": 20.0})

state, note = eng.submit_reading("主方案", Reading(
    t=5, ring=1, chainage=1.5, torque=2600, advance_speed=42,
    chamber_pressure=2.1, grout_volume=6.2))

eng.adjust("主方案", t=10, ring=1, author="李工",
           torque=(2000, 3400), advance_speed=(28, 40),
           chamber_pressure=(2.2, 3.0), grout_volume=(6.2, 8.5),
           stratum="clay")

# 重启后：Engine(EventStore("tunnel.jsonl")) 自动重放恢复全部方案
```
