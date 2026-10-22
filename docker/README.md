# 本地基础设施：Docker Compose

这个目录和根目录的 `docker-compose.yml` 只做一件事：**替代手工拉基础设施**。

- 以前：`docker run -d --name qdrant -p 6333:6333 qdrant/qdrant` + 本机装 MySQL。
- 现在：`docker compose up -d`（Qdrant）+ `docker compose --profile mysql up -d`（可选 MySQL）。

**应用进程仍然跑在宿主机**：`uvicorn`、`mvnw spring-boot:run`、`npm run dev` 一行都不用改。
把三个应用也塞进容器是第二档的事，见文末。

## 快速开始

```bash
# 在仓库根目录
docker compose up -d              # 只起 Qdrant
docker compose ps                 # 看状态
curl http://127.0.0.1:6333/collections
```

Qdrant 数据在 named volume `rag-system_qdrant_data` 里，删容器不会丢。

## 从现有的 `docker run` 容器切换（重要）

你之前用 `docker run` 起的容器叫 `qdrant`，**没有挂 volume**，数据在容器可写层里。
直接 `docker compose up -d` 会因为它占着 6333 而失败。两条路：

### A. 保留向量数据（推荐，数据不用重新入库）

```powershell
# 1. 停掉旧容器（先别删，数据还在里面）
docker stop qdrant

# 2. 把数据拷到宿主机
docker cp qdrant:/qdrant/storage ./qdrant-storage-backup

# 3. 删除旧容器
docker rm qdrant

# 4. 起 compose 的 Qdrant（创建 rag-system_qdrant_data 卷）
docker compose up -d qdrant

# 5. 把备份灌进 named volume
docker run --rm -v rag-system_qdrant_data:/dest -v "${PWD}/qdrant-storage-backup:/src:ro" alpine sh -c "cp -a /src/. /dest/"

# 6. 重启让 Qdrant 重新加载
docker compose restart qdrant

# 7. 确认 collection 还在
curl http://127.0.0.1:6333/collections
```

Git Bash 里把 `${PWD}` 换成 `"$PWD"`。确认无误后可以删掉 `qdrant-storage-backup/`
（已在 `.gitignore` 里）。

### B. 不要旧数据

```powershell
docker stop qdrant
docker rm qdrant
docker compose up -d qdrant
```

之后重新上传文档入库即可（会重新走 LLM 抽实体，比较慢）。

## MySQL（可选）

MySQL 是 `profiles: ["mysql"]`，默认**不会**启动。它占用宿主机 **3307**，
刻意避开本机已经在跑的 MySQL84（3306），两者可以共存。

```bash
# 可选：改密码/端口
cp .env.example .env

docker compose --profile mysql up -d
docker compose ps                    # 等 rag-mysql 变成 healthy
docker compose logs -f mysql         # 看初始化脚本有没有报错
```

首次初始化（`mysql_data` 卷为空）时会自动：

1. 建库 `computer_biz`；
2. 建用户 `computer_biz`，密码取 `.env` 的 `DB_PASSWORD`（默认 `123456`）；
3. 执行 `computer-biz-service/db/schema.sql` 建表（通过 `docker/mysql/init/10-schema.sql` 包一层 `USE`）。

让 Java 连这个容器：

```powershell
$env:DB_PORT = "3307"
$env:DB_PASSWORD = "123456"   # 必须和 .env 里的 DB_PASSWORD 一致
cd computer-biz-service
.\mvnw.cmd spring-boot:run
```

注意：`.env` 只给 compose 做变量替换，**不会自动导出到你的终端**，
所以 `DB_PORT` / `DB_PASSWORD` 还是要像上面这样单独设。

跑 `mvn test` 需要 `computer_biz_test`，初始化脚本不会建它：

```bash
docker compose exec mysql mysql -uroot -proot -e "CREATE DATABASE IF NOT EXISTS computer_biz_test DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; GRANT ALL PRIVILEGES ON computer_biz_test.* TO 'computer_biz'@'%'; FLUSH PRIVILEGES;"
```

## 常用命令

```bash
docker compose ps                       # 状态
docker compose logs -f qdrant           # 日志
docker compose restart qdrant
docker compose --profile mysql up -d    # 只加 MySQL
docker compose stop mysql               # 停 MySQL，保留数据
docker compose down                     # 删容器和网络，保留 named volume
docker compose down -v                  # ⚠️ 连 Qdrant 和 MySQL 的数据卷一起删
```

`down -v` 会把整个项目的两个卷都删掉（不只是 MySQL）。只想清 MySQL 数据时：
`docker compose stop mysql && docker volume rm rag-system_mysql_data`。

## 几个刻意的取舍

- **只用 named volume，不 bind mount Windows 目录**。Qdrant 是 Linux 进程；
  SQLite（`crawler_state/`）和文件锁在跨文件系统的挂载上语义不可靠。
  应用的数据目录仍然留在宿主机，因为应用本身就在宿主机跑。
- **Qdrant 镜像固定到 `v1.19.0`**，和现有容器一致，避免 `latest` 某天悄悄升级。
- **schema 只有一份**。容器通过 `SOURCE` 复用 `computer-biz-service/db/schema.sql`；
  已经初始化过的卷不会重跑，改表要手动 `ALTER`，或删卷重建（会丢数据）。
- **MySQL 端口默认 3307**。想让容器顶替本机 MySQL 时，把 `MYSQL_HOST_PORT` 改成 3306
  并停掉本机服务即可，但两边的数据不会自动迁移。

## 第二档：把应用也容器化

需要给 Python / Java 各写 `Dockerfile`，前端 `npm run build` 后交给 nginx。
这一档必须改三处服务间地址（容器里的 `localhost` 是容器自己）：

| 配置 | 宿主机模式 | Compose 模式 |
|---|---|---|
| Java `rag.base-url` | `http://localhost:8102` | `http://rag-api:8102` |
| Python `QDRANT_URL` | `http://localhost:6333` | `http://qdrant:6333` |
| Python `AGENT_BIZ_QUERY_URL` | `http://localhost:8080/...` | `http://biz-service:8080/...` |

另外：`Lightrag_db/`、`chat_history/`、`crawler_state/`、`uploads/` 要挂 volume；
`.env` 用 `env_file:` 注入，不要 `COPY` 进镜像；爬虫建议继续留在宿主机
（Windows 计划任务和电池条件不变），它只通过 HTTP 访问 API。

在第二档落地前，`start.md` 的三条本地命令仍然是日常开发方式。
