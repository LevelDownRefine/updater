# updater

基于 GitHub Releases 的多文件程序更新库，Python 3.11+。支持普通 ZIP、断点续传、
取消、SHA-256 校验、文件替换、失败回滚和中断恢复；不要求 Qt、PyInstaller 或特定前端。

```python
from pathlib import Path
from updater import GitHubUpdater

updater = GitHubUpdater(
    repository="owner/repo",
    asset_name="App.zip",
    install_dir=Path("installed"),
    current_version="1.2.3",
    archive_prefix="App/",  # ZIP 无顶层目录时省略
    manifest_name="update-manifest.json",  # 可选：启用逐文件部分更新
)
release = updater.check()
if release is not None:
    prepared = updater.download(release)
    # 应用退出后，在独立更新进程中执行；该进程须位于安装目录之外。
    updater.install(prepared)
```

## 发布格式

在 GitHub Release 上传普通 ZIP 即可。稳定版本 tag 可以是 `1.2.3` 或 `v1.2.3`。
库不要求发布 `version.json`、`update-manifest.json` 或专用校验附件。

指定 `manifest_name` 后，库先按 HTTP Range 读取 ZIP 目录和清单。本地文件的
SHA-256 与清单一致时复制到暂存目录，只下载新增或变化文件；已完成区间和验证过的
暂存文件跨次复用。清单缺失、HEAD/Range 不受支持或没有稳定 HTTP validator 时，
回退整包断点下载。未指定清单时直接使用整包模式，不探测远端 ZIP。

清单为 UTF-8 JSON：`{"schema":1,"version":"1.2.4","files":{"app.exe":"64位小写SHA-256"}}`。
路径相对于 ZIP 前缀，列出全部文件但不包含清单自身；额外元数据允许存在。
库核对版本、文件范围及每个文件的哈希；部分下载不具有整个 ZIP 的字节，因此不能
验证 GitHub 的整包 digest，完整性依据 HTTPS 获取的清单和逐文件 SHA-256。
清单损坏或内容不匹配明确报错，不降级绕过校验。

`check()` 仅查询最新稳定 Release 并按附件名选择 ZIP，没有更新或没有 Release 时
返回 `None`；缺少指定附件等错误显式报错。它不读安装目录，不写缓存，也不检查
`sys.frozen`。版本和安装目录由调用方明确提供。

整包模式下，GitHub 附件提供 SHA-256 digest 时，下载完成后必须匹配该值；附件没有 digest 时
仍验证声明的下载大小以及 ZIP 的 CRC。后者不等于来源认证，发行来源依赖 HTTPS
和 GitHub 仓库权限。

## 下载与断点

`download()` 始终是同一个接口。进度回调接收 `(received_bytes, total_bytes)`；
`cancelled` 接收 `threading.Event`，取消时抛 `UpdateCancelled`。整包网络异常直接传回；
部分模式的网络/缓存 I/O 中断抛 `UpdateError` 子类并保留原异常，避免 ZIP 解析器误吞网络错误。
取消和断网都保留已落盘的字节；再次调用或重新构造相同参数的客户端会继续下载。

库按仓库、附件 ID、URL、版本、大小、digest 和 ZIP 前缀区分缓存。续传使用 HTTP
`Range`，并在可用时发送 ETag／Last-Modified 的 `If-Range`。远端不支持续传或返回
整包时重新下载；错误的区间或变化的 validator 明确拒绝，不拼接不同版本的字节。
没有 validator 和 SHA-256 时从头下载，避免无法验证的续传。
部分模式的进度先以归档大小为上限，完成时按实际下载字节报告 `(received, received)`；
复用本地文件不计入网络流量。

`PreparedUpdate` 只在完整下载、校验及解包结束后返回。它可以跨进程传递目录和版本；
安装会重新验证本地文件及安装目录身份。调用 `updater.discard(release)` 才明确删除
该附件的下载缓存，包括已安装版本或更早版本的缓存；清理不删除程序及安装清单。
工作目录统一位于安装目录的 `.update/`。

## 安装与本地数据

仅替换 ZIP 中的文件，目录结构无需调整：example 和任务声明随 ZIP 更新，实际用户
配置、日志及其他未发布的本地文件保留原位。不要把实际用户配置放入发布 ZIP。
内容哈希和权限均未变化的文件不替换，也不创建回滚快照；新的安装清单仍包含全部发布文件。

首次安装只替换同名发布文件，不猜测哪些历史文件属于程序。库在 `.update/` 中记录
已安装文件清单；后续更新只清理旧清单拥有、新包不再发布的文件。被本地修改的旧文件
不删除并明确报错。后续新增发布文件与清单之外的本地文件同名时，在修改任何程序
文件前拒绝安装，避免覆盖用户内容。第一版接入前遗留的文件不会被自动清理。

替换前创建并刷盘旧文件快照；失败恢复旧文件和旧清单。进程中断后，在应用重新启动前
调用 `updater.recover()` 恢复。多更新进程通过安装目录锁互斥。

调用方负责检查任务是否运行、关闭 GUI／CLI／运行器、安排独立更新进程、阻止更新时
重新启动应用，以及安装后重启。库不推断前端父子关系，不检查 EXE 名称，不杀进程，
也不改变管理员权限。不要在仍持有程序文件的主进程里调用 `install()`。

## 开发

```bash
uv sync --locked
uv run python -m unittest discover -s tests -t .
uv run ruff check .
uv run ruff format --check .
```

源码直接位于 `src/`；通用路径、校验、锁和文件操作集中于 `src/utils.py`。
打包配置将 `src/` 映射为安装后的 `updater` 包，避免与调用方自身的 `src` 包冲突。
CI 只测试 Python 3.13；最低支持版本仍为 Python 3.11。

测试使用临时目录、合成应用和本地 HTTP 服务，不访问真实安装或用户配置。
