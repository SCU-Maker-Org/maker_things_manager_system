# 物资管理系统 (Things Manage System v1.1.1)

基于 Flask + Flask-SQLAlchemy 的物资管理系统，支持用户注册登录、物资目录检索、批量申领、审批流转、归还审核、库存导入导出和 Docker 部署。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

访问地址：`http://127.0.0.1:5000`

开发环境默认会加载演示数据（`SEED_DEMO_DATA=1`），演示账号：
- 管理员：`admin / 123456`
- 普通用户：`user1 / 123456`、`user2 / 123456`

生产环境不会创建上述演示账号。

## 测试

```bash
python -m unittest discover
```

测试默认使用内存 SQLite，不会改动 `instance/storage.db`。

## 目录结构

```text
.
├── app.py                    # 本地开发兼容入口，支持 python app.py
├── wsgi.py                   # Gunicorn/生产部署入口
├── src/things_manager/       # Flask 应用源码包
├── src/things_manager/app.py # 应用、模型、路由和初始化逻辑
├── src/things_manager/templates/
├── instance/                 # SQLite 数据库与运行时数据
├── Dockerfile
├── docker-entrypoint.sh      # 生产容器单次初始化入口
├── requirements.txt
├── tests/
├── .env.example
└── README.md
```

## 生产部署

构建镜像：

```bash
docker build -t things-system:v1.1.1 .
```

创建持久化数据库目录：

```bash
mkdir -p /data/things_system/instance
```

启动容器：

```bash
docker run -d \
  -p 5000:5000 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e BOOTSTRAP_ADMIN_PASSWORD='请替换为至少12位的强密码' \
  -v /data/things_system/instance:/app/instance \
  --name things_manage_prod \
  --restart always \
  things-system:v1.1.1
```

健康检查地址：`http://服务器IP:5000/healthz`。生产登录流量应通过 HTTPS 反向代理接入。

## 关键配置

- `SECRET_KEY`：生产环境必须设置随机强密钥。
- `APP_ENV`：运行环境；生产镜像固定为 `production`，会强制校验密钥。
- `DATABASE_URL`：可选，默认使用 `sqlite:///storage.db`，数据库文件位于 Flask instance 目录。当前镜像仅内置 SQLite 驱动；切换外部数据库前需安装对应驱动并引入正式迁移流程。
- `PORT`：可选，本地 `python app.py` 启动端口，默认 `5000`。生产容器使用 `wsgi:app`。
- `AUTO_INIT_DB`：本地默认 `1`。生产镜像固定为 `0`，由入口脚本在 Gunicorn worker 启动前统一初始化一次。
- `QUIET_INIT_DB`：可选，设为 `1` 时隐藏初始化种子数据提示。
- `SEED_DEMO_DATA`：是否创建演示账号和物资；开发默认 `1`，生产镜像默认 `0`。
- `BOOTSTRAP_ADMIN_PASSWORD`：空生产数据库首次启动时必填，长度须为 12–128 位；也可配置管理员用户名和显示名。
- `SESSION_COOKIE_SECURE`：生产默认 `1`，仅通过 HTTPS 发送会话 Cookie；本地开发默认 `0`。
- `.env.example`：环境变量示例文件，部署时可按需复制为 `.env` 或写入服务器环境。

## 上线注意

- SQLite 部署只运行单个应用容器；多副本部署应先迁移到服务型数据库，并将初始化改为独立部署任务。
- 在 HTTPS 反向代理或 API 网关上对登录和注册接口设置按 IP/账号限速，降低撞库、批量注册和密码哈希 CPU 滥用风险。
- 定期备份挂载的 `instance/` 目录，并在恢复演练后再升级应用镜像。

## 功能概览

- 管理端：库存入库、分类维护、CSV 导入导出、借用审批、归还核销、低库存和维修状态看板。
- 用户端：物资目录检索、暂存箱批量申领、额度校验、申请撤回、归还申请、个人借用记录。
- 安全基础：密码哈希存储、登录态校验、管理员视图隔离、API 会话失效 JSON 响应。
