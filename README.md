# 互动编辑的多人分支合流与观众交付

节目时间用整数帧位置。编辑目标之间的约束由节目契约指定，消息中的媒体区间含起点、不含末点。

本仓库在消息契约与读取器之外，提供了一条**可重复使用的 Python 编辑消息通道**
（标准库实现，Python 3.11）：协调多人共用的节目时间线，能合流的编辑尽快进入候选/发布，
不能一起应用的冲突留给值班制作方决策；同时管理转播方的领取、应用确认、撤回、重连与跟进切换。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **program**　节目 program、branch、初始 base、splices 及 tied 模型。tied 中各组目标随同一冲突边界考虑。
- **edit**　id、program、branch、base、span、target 与 change。可带 `by`/`submitter` 表示制作台或主持人，可带 `"draft": true` 先存草稿。
- **decide**　制作方确认 accept 的编辑形成发布、拒绝 reject，也可用 `resubmit` 要求相关提交者按较新节目重新提交。`base` 为当前候选依赖的节目版本；未另行给出预约媒体点（`effective_media`/`media` 等）时，以目标区间可用的首个拼接点安排。每个节目分支发布 revision 从 1 递增。默认 `policy="bundle"` 会把已通过冲突检查的干净候选编辑随本次 decide 一并发布（尽快合流）；`--policy accept_only` 时仅发布 accept 列表。
- **cast**　订阅方为 branch 领取可应用发布（标记为“已读/已领取”，尚欠 applied 确认）。
- **applied**　订阅方确认已经应用 revision，携带 media 位置。
- **retract**　制作方撤回对应发布 revision；系统按转播方是否已明确使用分别处理。
- **resume**　订阅方重连并提交自己所见 revision 与 media，返回“继续用哪一版、何时更换、哪些已读版本仍欠确认”。
- **inspect**　查看 edit（`{"edit":"id"}`，返回完整经历时间线）或 cast（`{"cast":"name"}`，返回重连/确认视图）。

## 示例

- two-targets-tied.jsonl　tied 目标 + 半开区间边界 + 独立编辑合流
- partial-live-withdraw.jsonl　部分转播方已播出后的撤回与重连
- resubmit-and-follow-switch.jsonl　草稿、冲突退回重提、后继版本与跟进切换

## 快速运行

运行环境为 Python 3.11，只使用标准库。在根目录运行：

```bash
# 核对文件格式及读取接口（原有）
python3 scenario_reader.py examples
python3 -m unittest discover -v

# 回放单个场景，逐消息输出 JSON 回执
python3 -m edit_channel examples/two-targets-tied.jsonl

# 中文演练文本；目录会回放全部 *.jsonl
python3 -m edit_channel examples --text

# 自检式 Python API + 本地转播端演示
python3 demo.py
```

等价入口：`python3 simulate.py ...`、`python3 main.py ...`、`python3 channel.py ...`。
`desk.py`、`program_desk.py`、`scenario_runner.py` 是导入/运行兼容别名。

## 作为 Python 库复用

```python
from edit_channel import EditChannel, LocalBroadcaster

desk = EditChannel()                 # 同一个“项目/节目台”对象
desk.handle({"at": 0, "kind": "program", "record": {...}})
receipt = desk.handle({"at": 1, "kind": "edit", "record": {...}})

# 三个核心入口（也都有 submit_edit / resolve_conflict / claim 等别名）
desk.submit_edit({...})              # 编辑提交
desk.decide({"program": "P", "accept": ["a"], "reject": ["b"], "resubmit": [...]})
desk.cast({"name": "A", "program": "P", "branch": "full"})
desk.applied({"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 11})

# 查询：一条编辑经历了什么
desk.inspect_edit("a")               # 含 history 事件时间线 / conflicts / released_in
desk.trace_edit("a")                 # 只取事件列表

# 查询：转播方重连视图
desk.resume({"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 16})

# 本地模拟转播端（封装 cast/applied/resume 与播放头、媒体标识）
a = LocalBroadcaster(desk, "A", "P", "full")
a.cast(); a.seek(11); a.apply(1)
print(a.media_id)                    # urn:media:P/full#11

# 项目状态可持久化/恢复
desk.save_state("project.json")
EditChannel.load_state("project.json")
```

## 关键规则的落地方式

1. **冲突边界 = 半开区间严格相交 + 目标不独立。**
   `[20,40)` 与 `[40,60)` 只相接、不相交。目标仅当“相同”或“同属一个 `tied` 绑定组”
   时才不独立；因此人物遮挡与同一人物覆盖层/字幕即使改的字段不同，只要区间相交就判冲突，
   回执给出 `overlap` 相关区间、`with` 先前编辑身份、`tied` 关系，以及若已发布则有
   `other_revision` / `since_base`。
2. **旧基准先识别范围。** 基准解析支持 `v0`、整数、`r1`/`rev1`/`v1`。不相交或目标独立的
   编辑保持 `staged`（在候选中，head 前进时记录 `candidate_rebased`）；冲突编辑为
   `conflicted`，值班 `decide` 选择 accept/reject，或用 `resubmit` 把二位退回并附上
   当前应重基的 revision。
3. **发布与拼接点。** decide 形成分支上递增的 revision；默认把当前所有干净候选与点名
   accept 的胜方组成同一完整版本（可用 `defer` 暂留）。未给预约媒体点时取目标并集
   `[lo,hi)` 内首个 splice，没有则取不早于 `lo` 的首个 splice 并告警；显式媒体点必须是
   节目声明的 splice。互相冲突的编辑不能同时 accept（整次决策原子拒绝，不产生 revision）。
4. **领取与已读欠确认。** cast 会把该转播方链条上可应用的发布标记为已读；
   `unconfirmed_read`/`pending_confirmations` 即“已读但仍欠 applied 确认”。
5. **顺序不可跳过。** applied 更高 revision 时，若同一转播方还有对其有效的、未确认的
   前置 revision，会以 `predecessor_unconfirmed` 拒绝并给出 `missing_revisions`；
   已被撤回并对该转播方收回的版本不算缺失，可以合法越过。
6. **撤回的部分生效语义。** retract 时按分支订阅者分区：
   - 未 applied、也未上报正在使用该 revision 的转播方 → `reclaimed_for`，待播版本收回，
     继续 `continue_revision=0/上一确认版`，不再欠该版本确认；
   - 已 applied（或 resume 上报该 revision）的转播方 → `follow_switch_for`，播出历史保留，
     不能回写为未播，生成 `follow_up`；后继 revision 一确认就写入
     `follow_switch_decisions`，并在重连/检查视图给出未来 splice 的 `next_switch`。
7. **切换点与播放头。** `next_switch.switch_at_media` 通常等于发布预约 splice；若该点
   已不晚于转播方上报的 media（错过了预约点），自动改约播放头之后的第一个节目 splice，
   并置 `missed_scheduled_point=true`。
8. **非订阅者不等待。** 所有撤回/发布路由只枚举该 `(program, branch)` 的订阅转播方；
   其他分支或从未订阅的名称不进入回执与等待集合。
9. **媒体标识。** 整数 media 位置之外，统一生成稳定 URN：
   时间线 `urn:media:P/full`、帧 `urn:media:P/full#20`。

## 输出回执

CLI 每行输出一条消息的处理结果：

```json
{"ok": true, "at": 3, "kind": "inspect", "result": { ... }}
```

业务规则错误（缺字段、未知 program/base/revision、跳版本确认、非 splice 预约点等）
被收敛为 `"ok": false` 且带 `error_code`/`detail` 的回执，回放不中断；
文件/JSONL 契约错误由读取器报出“第 N 行”。加 `--strict` 时存在业务错误回执则退出码为 1。

## 目录结构

- `scenario_reader.py`：原有信封校验读取器（保持不变）。
- `edit_channel/`：通道实现。
  - `core.py`：`EditChannel`（别名 `Project`/`ProgramDesk`/`Desk`/`Channel`）与全部规则。
  - `spans.py`：半开区间、splice 计算、媒体 URN。
  - `simulator.py`：JSONL 回放与 `LocalBroadcaster`/`SimulatedBroadcastEndpoint`。
  - `render.py`：中文文本回执；`cli.py`/`__main__.py`：命令行入口。
- `test_edit_channel.py`：覆盖上述全部规则与两个随仓示例；原有读取器测试保持通过。
