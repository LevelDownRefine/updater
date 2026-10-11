# updater

从 OneDragon-Helper 拆出的独立更新库，Python 3.11+，不依赖 Qt、助手的配置工具或任务执行器。

下载、SHA-256 校验、运行锁、安装事务、失败回滚与中断恢复由本库负责。应用通过
`UpdatePolicy` 提供 GitHub 仓库、发布附件名、ZIP 顶层目录、必要文件、程序文件白名单、
进程路径及重启参数；没有默认应用，也没有全局可变配置。

```python
from pathlib import Path
from updater.service import UpdateService

# policy 由应用声明，参见 tests/support/policy.py 的完整合成示例。
client = UpdateService(Path("installed"), policy=policy)
release = client.check_update()  # 显式联网；仅正式冻结构建支持原位更新
if release is not None:
    prepared = client.prepare_update(release)
    client.start_update(prepared)  # ready 后调用方必须退出，安装器才替换文件
```

独立安装器入口随应用打包：

```python
from updater.worker import main
from application.update_policy import POLICY

if __name__ == "__main__":
    raise SystemExit(main(POLICY))
```

该入口接收 `--root`、`--package` 或 `--recover`，以及 PID／创建时间、就绪回执、取消、
结果路径与 `--restart`；安装器从安装目录之外的副本运行，等待调用进程退出，不终止已有任务。
应用启动入口通过 `application_lease(root, policy=policy)` 持有共享锁。

| 模块 | 职责 |
| --- | --- |
| policy | 应用提供的发布与布局规则 |
| package | schema 1 清单、路径边界、哈希及本地／远端 ZIP 流式解包 |
| remote | remotezip 范围读取、取消与区间缓存 |
| runtime | 更新闸门、共享运行锁、按完整路径识别应用进程 |
| installer | 文件替换、持久化事务、回滚与恢复 |
| service | 稳定 Release 检查、下载准备与安装交接 |
| worker | 无 GUI 的安装器命令行入口 |

协议文件固定为 `update-manifest.json` 与 `version.json`；清单必须列出所有程序文件，
不得包含用户数据。保留 `frontend` 字段用于发布布局识别，其值由 policy 定义；省略时采用
`default_frontend`。库始终拒绝路径穿越、Windows 路径别名、链接和 `.update` 工作目录，
应用白名单无法绕过这些边界。哈希提供完整性校验，发行来源仍依赖 HTTPS 和仓库权限。

开发与验证：

```bash
uv sync
uv run python -m unittest discover -s tests -t .
uv run ruff check .
uv run ruff format --check .
```

测试使用临时目录、合成应用和本地 HTTP 服务；不访问真实安装或用户配置。Windows
冻结产物及 Qt／Rust 前端交接的集成测试留在调用方 OneDragon-Helper。
