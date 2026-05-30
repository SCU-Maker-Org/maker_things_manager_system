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

默认账号：
- 管理员：`admin / 123456`
- 普通用户：`user1 / 123456`、`user2 / 123456`

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
  -v /data/things_system/instance:/app/instance \
  --name things_manage_prod \
  --restart always \
  things-system:v1.1.1
```

健康检查地址：`http://服务器IP:5000/healthz`

## 关键配置

- `SECRET_KEY`：生产环境必须设置随机强密钥。
- `DATABASE_URL`：可选，默认使用 `sqlite:///storage.db`，数据库文件位于 Flask instance 目录。
- `PORT`：可选，本地 `python app.py` 启动端口，默认 `5000`。
- `AUTO_INIT_DB`：可选，默认 `1`。设为 `0` 可跳过启动时自动建表和种子数据初始化。

## 功能概览

- 管理端：库存入库、分类维护、CSV 导入导出、借用审批、归还核销、低库存和维修状态看板。
- 用户端：物资目录检索、暂存箱批量申领、额度校验、申请撤回、归还申请、个人借用记录。
- 安全基础：密码哈希存储、登录态校验、管理员视图隔离、API 会话失效 JSON 响应。
