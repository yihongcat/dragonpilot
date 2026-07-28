# 上车前测试流程

这份流程用于所有准备推送到 openpilot 设备、或者准备实车测试的修改。目标是在上车前发现构建、启动、进程通信、无驾驶员摄像头兼容和纵向规划问题，减少反复上下车测试。

## 固定约束

- 所有源代码只在本地工作区修改。
- 设备端不直接修改代码；设备只读取日志，或在明确要求后拉取已经推送的分支。
- `test/no-driver-cam-fix` 与 `merge/comma-master` 始终分开，除非用户明确要求合并。
- 不启用或测试 USB GPU 大模型功能。
- 未完成相应测试门禁时，不建议进行实车测试。
- 每次必须记录通过、失败和跳过的项目；不能把部分通过表述为全部通过。

## 快速调用

以后可以直接对 Codex 说：

> 执行上车前测试 quick

或：

> 按 PRE_DRIVE_TESTING.md 对 merge/comma-master 执行完整上车前测试

在 Linux 或 WSL 的项目根目录也可以直接运行：

```bash
bash scripts/pre_drive_check.sh quick
bash scripts/pre_drive_check.sh build
bash scripts/pre_drive_check.sh replay
bash scripts/pre_drive_check.sh sim
bash scripts/pre_drive_check.sh all
```

Windows 上优先使用已经固定依赖的 Docker 环境：

```powershell
.\scripts\pre_drive_docker.ps1 quick
.\scripts\pre_drive_docker.ps1 build
.\scripts\pre_drive_docker.ps1 replay
.\scripts\pre_drive_docker.ps1 sim
.\scripts\pre_drive_docker.ps1 all
```

Docker 入口会自动构建 `dragonpilot-pre-drive` 测试镜像，把 Linux 测试工作区、Python 虚拟环境、uv 下载缓存和 SCons 缓存保存在 Docker volume 中。Windows 项目目录以只读方式传入，容器从当前本地 commit 创建独立的 LF 工作副本，因此不会修改 Windows 工作树，也不会受到 CRLF 和 NTFS 权限差异影响。

Docker 测试要求工作树干净：先把准备测试的内容提交到本地 commit，再运行测试。这个 commit 不会被脚本自动推送；测试失败时仍可以继续在本地修改。

各模式的用途：

| 模式 | 内容 | 常用场景 |
|---|---|---|
| `quick` | Git/LFS 完整性、无驾驶员摄像头能力测试、弯道减速单元测试 | 每次代码修改 |
| `build` | `quick` 加完整本地 SCons 构建 | 合并官方 master、目录或 C++ 改动 |
| `replay` | `quick` 加 Toyota 的 `card/selfdrived/controlsd/plannerd` 官方路线回放 | 车辆接口、启用链、纵向规划改动 |
| `sim` | `quick` 加官方 MetaDrive 自动驾驶测试 | manager、进程启动和启用链改动 |
| `all` | 上述所有本地检查 | 准备推送给设备或实车验证 |

第一次执行会由 `uv` 准备 Python 3.12 环境并下载依赖。路线回放和 MetaDrive 首次运行时间较长，之后会使用缓存。

Docker Desktop 使用 WSL2 Linux 后端。程序和容器数据应保存在 D 盘，避免占满系统盘；可以用下面命令确认：

```powershell
docker version
docker info
docker run --rm hello-world
```

## Gate 0：分支和改动范围

执行前记录：

```bash
git status --short --branch
git log -1 --oneline --decorate
```

通过标准：

- 当前分支与测试目标一致。
- 没有把 `test/no-driver-cam-fix` 意外合并进 `merge/comma-master`。
- 所有未提交改动都能解释，并且没有覆盖用户原有改动。

## Gate 1：快速静态和单元测试

由 `quick` 模式执行：

1. `git diff --check HEAD`
2. `git lfs fsck`
3. 确认 `driving_supercombo.onnx` 是完整 LFS 文件，而不是指针文本。
4. 无驾驶员摄像头能力判断测试。
5. 弯道距离预判、提前减速和 jerk 限制测试。

通过标准：

- Git diff 没有空白或冲突标记错误。
- LFS 校验通过，驾驶模型文件大于 1 MB。
- 所有目标单元测试通过。

## Gate 2：完整构建

由 `build` 或 `all` 模式执行：

```bash
uv run --python 3.12 scons -j4
```

通过标准：

- SCons 返回成功。
- 没有 Python 导入、C/C++、Panda safety、UI 资源或模型解析错误。

任何上游 master 合并都必须执行此门禁。

## Gate 3：真实路线进程回放

由 `replay` 或 `all` 模式执行。当前固定回放：

- `card`
- `selfdrived`
- `controlsd`
- `plannerd`
- Toyota 官方回放路线

它用于发现：

- 车辆识别或 `CarParams` 无法产生。
- `selfdriveState` 不输出。
- 控制和规划消息发生非预期变化。
- 弯道减速修改影响其他纵向逻辑。

后续应固定保存一段本车 `TOYOTA_RAV4_TSS2` 的有效路线，作为项目专用回放样本。专用样本至少应包含：

- 正常启用和退出。
- 无驾驶员摄像头。
- 直道进入明显弯道。
- Blend 模式且未开启 AEM。
- 入弯前至少 10 秒的模型预测和车辆速度。

弯道功能额外通过标准：

- `target_distance` 随车辆接近弯道而合理减小。
- `required_decel` 在超过弯道速度上限以前已经大于零。
- 输出减速度遵守 jerk 限制，不在边界处突变。
- `dp_curve_speed` 日志包含有效目标速度、距离、所需减速度和最终应用减速度。

## Gate 4：官方 MetaDrive 模拟驾驶

由 `sim` 或 `all` 模式执行。官方模拟器入口位于：

```text
openpilot/tools/sim/
```

模拟测试必须确认：

- manager 能正常启动上路进程。
- `card` 能发布 `carState` 和 `CarParams`。
- `selfdrived` 在限定时间内开始持续发布 `selfdriveState`。
- `managerState` 中没有应运行但已停止的关键进程。
- 没有阻止启用的通信或初始化事件。
- openpilot 能进入 active 状态并保持至少 100 个控制周期。

无驾驶员摄像头专项场景应使用道路和广角道路摄像头、但不提供驾驶员摄像头，并确认：

- `DriverCameraPresent=false`。
- `dmonitoringd` 和 `dmonitoringmodeld` 不被要求运行。
- `driverCameraState` 和 `driverMonitoringState` 不阻止 selfdrived 初始化。
- 不出现由缺少驾驶员摄像头引起的 `cameraMalfunction` 或 `commIssue`。

## Gate 5：设备离车检查

本地 `all` 通过并推送后，才进入本门禁。此阶段不需要上车或点火。

1. 设备拉取指定分支。
2. 核对设备 HEAD 与远端目标 commit 一致。
3. 等待完整构建明确显示成功，不能在构建过程中重启。
4. 重启后等待 manager 和 UI 稳定至少 60 秒。
5. 检查关键进程没有反复退出。
6. 检查模型文件大小、LFS 状态和启动错误。
7. 确认设备工作树只有已知的运行时兼容目录，没有源代码修改。

如果设备仍在构建，或者 manager 尚未启动，禁止立即上车测试。这会导致 UI 在上路后显示：

```text
openpilot Unavailable
Waiting to start
```

## Gate 6：实车测试

只有适用的 Gate 0–5 全部通过后才进行。

第一次实车测试保持短距离、低速、随时可人工接管，依次检查：

1. 点火后及时出现 `selfdriveState`，不再显示“等待开始”。
2. 车辆型号和纵向控制能力识别正确。
3. 可以正常启用、退出和重新启用。
4. 无驾驶员摄像头不会触发通信或摄像头故障。
5. 再测试弯道提前减速，并保存完整路线日志。

实车仍不可替代的部分包括 Panda/CAN 实际通信、车辆 ECU 行为、安全固件匹配、真实摄像头时序以及最终制动体感。

## 测试结果存档模板

每次测试在任务中按下面格式报告：

```text
分支：
commit：
改动范围：

Gate 0 分支/范围：PASS / FAIL
Gate 1 快速检查：PASS / FAIL
Gate 2 完整构建：PASS / FAIL / SKIP
Gate 3 路线回放：PASS / FAIL / SKIP
Gate 4 MetaDrive：PASS / FAIL / SKIP
Gate 5 设备离车：PASS / FAIL / SKIP
Gate 6 实车：PASS / FAIL / NOT RUN

失败或跳过原因：
是否允许进入下一阶段：
```
