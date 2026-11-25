# 互动编辑的多人分支合流与观众交付

节目时间用整数帧位置。编辑目标之间的约束由节目契约指定，消息中的媒体区间含起点、不含末点。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **program**　节目 program、branch、初始 base、splices 及 tied 模型。tied 中各组目标随同一冲突边界考虑。
- **edit**　id、program、branch、base、span、target 与 change。
- **decide**　制作方确认 accept 的编辑形成发布、拒绝 reject。base 为当前候选依赖的节目版本；未另行给出预约媒体点时，以目标区间可用的首个拼接点安排。每个节目分支发布 revision 从 1 递增。
- **cast**　订阅方为 branch 领取可应用发布。
- **applied**　订阅方确认已经应用 revision，携带 media 位置。
- **retract**　制作方撤回对应发布 revision。
- **resume**　订阅方重连并提交自己所见 revision。
- **inspect**　查看 edit 或 cast。

## 示例

- two-targets-tied.jsonl
- partial-live-withdraw.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
