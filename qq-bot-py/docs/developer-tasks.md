# GPT 开发任务

管理员可在控制台「开发任务」提交需求，也可私聊机器人发送 `#开发 需求`。任务先复制允许的源码到独立目录，模型只读取该副本。配置、SFTP、数据、人设、私钥、依赖目录和检测到含凭据的源文件不会进入副本。

任务最多进行 8 轮模型调用，单轮 180 秒、累计执行 15 分钟。进度、调用记录、完整差异和检查结果持久保存；重启从最近检查点恢复。差异通过检查后等待管理员审批，有效期 24 小时。审批时再次核对整个源文件树及差异的哈希；源码变化会拒绝旧审批。

## 准备隔离检查镜像

在 Linux 宿主机的项目目录执行一次。构建只需要依赖清单和 Dockerfile，使用临时目录避免发送配置、聊天数据和大型资源：

```sh
rinbot_check_context=$(mktemp -d /tmp/rinbot-dev-build.XXXXXX)
cp requirements.txt "$rinbot_check_context/"
cp tools/Dockerfile.developer-checks "$rinbot_check_context/Dockerfile"
docker build --progress=plain -t rinbot-dev-checks:local "$rinbot_check_context"
```

默认从 PyPI 下载依赖。如果网络较慢，可中断当前构建，在同一终端使用[清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)重新构建：

```sh
docker build --progress=plain \
  --build-arg PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
  -t rinbot-dev-checks:local "$rinbot_check_context"
```

`--progress=plain` 保留完整构建日志，不会只显示最后几行。第一次构建需要下载完整 Python 依赖；下载大包时短暂无新包名不代表卡死。Dockerfile 为网络读取设置 30 秒超时和 3 次重试，该超时不是整个下载或构建的总时限。出现镜像导出完成且命令退出成功后，再启用开发任务。

机器人宿主机需要能调用 Docker CLI 与本机 Docker 服务。检查容器不接收 Docker socket、宿主目录、生产配置或环境变量；源码通过 `docker cp` 复制到从未启动的中转容器，再提交为临时镜像。真正执行检查的容器使用只读根目录并禁用网络，以非 root 身份运行，内存上限 512 MiB、CPU 上限 1 核、进程数上限 64，单项检查最多 5 分钟。完成后清理两个容器和临时镜像。没有可用 Docker 或镜像时，任务显示检查失败，不会改在宿主机运行生成代码。

默认必须通过差异空白检查和 Python 编译检查。模型可追加 pytest；测试需要的数据库、Redis 和配置应使用项目的离线测试夹具，不能依赖生产服务。若增加依赖，需要管理员重新构建镜像后创建新任务。

公开部署默认关闭开发任务。先配置 `ai.openai_responses` 的接口、模型和环境变量密钥，并确认宿主机可以运行上述隔离镜像；再按实际管理员填写下面的配置：

```yaml
features:
  development_agent: true
agent:
  model_routes:
    development:
      enabled: true
      provider: openai_responses
      protocol: responses
      timeout: 180
      max_output_tokens: 12000
  dev:
    enabled: true
    workspace: .
    task_storage: data/developer-tasks
    check_image: rinbot-dev-checks:local
    admin_users: [123456789]
    # 多管理员时，指定哪个 QQ 管理员对应控制台账号。
    console_admin_user: 123456789
    console_username: admin
```

只配置一个 QQ 管理员时，默认将 `admin` 控制台账号与此管理员关联。多个 QQ 管理员时必须明确指定映射，才可跨界面批准同一人的任务；模型和 HTTP 请求不能自行指定审批身份。没有映射时，任务只能由创建它的同一控制台账号或 QQ 管理员批准。

## 审阅与应用

在控制台阅读完整差异和检查结果后，勾选已审阅再批准。也可由同一管理员发送 `#agent approve 任务编号` 或 `#agent reject 任务编号`。这两个指令不接受旧版临时 Redis 审批。

应用前记录持久恢复日志，每个文件通过临时文件原子替换。多文件过程中发生错误会恢复已修改文件；进程中断后恢复任务会检查文件内容，遇到其他人随后修改的文件则停止并要求人工审阅。回滚副本保存在任务私有目录，可在控制台回滚，回滚也要求源文件树未再改变。

应用成功后不会自动重启机器人、上传 SFTP、提交 Git 或部署。请根据检查结果安排自己的发布步骤。任务目录包含开发需求和代码差异，应与数据库一同备份并保持私有。
