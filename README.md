# 物资管理系统 (Things Manage System v1.1.1) 容器化部署说明书

本系统基于 **Flask + Flask-SQLAlchemy** 架构开发，全面支持 **Docker + Nginx** 工业级容器化弹性部署。
本次版本已完成全流闭环重构，核心修复了前端 JavaScript 动态 DOM 表单（isEditMode）在新建二级细分类时的路由代差（404 隐患），全面上线了**新用户自主注册、密码 pbkdf2:sha256 强哈希加盐加密机制**以及**分类清洗时资产无损容灾收容机制**。

---

## 📂 源码交付目录清单
部署前请核对根目录下是否包含以下核心生命周期文件：
- `app.py`：后端路由与身份验证控制总线
- `templates/`：前端全套交互视图与样式矩阵
- `Dockerfile`：精简版基础环境装箱说明书
- `requirements.txt`：Python 全套第三方依赖账本

---

## 🛠️ 服务器环境要求
- 安装有 **Docker Engine** (推荐 20.10 及以上版本)
- 安装有 **Nginx**（用于公网 80 端口高并发反向代理与物理隔离）
- 服务器防火墙对内/对外放行端口：`5000` (后端)、`80` (公网访问)

---

## 运维一键部署

请将源码解压至服务器任意工作目录（如 `/home/admin/things_system`），在包含 `Dockerfile` 的根目录下执行以下命令，全自动在本地搬运底座并完成编译：
```bash
docker build -t things-system:v1.1.1 .

为了保障一二级分类标签、账号权限及入库物资数据的绝对安全（防止容器重启或升级时数据被抹除），必须在服务器物理机上创建一个专用的物理存储锚点
mkdir -p /data/things_system/instance

执行以下标准指令启动容器（已配置端口映射、数据卷飞线桥接与生产级自愈重启策略）：
docker run -d \
  -p 5000:5000 \
  -v /data/things_system/instance:/app/instance \
  --name things_manage_prod \
  --restart always \
  things-system:v1.1.1
```

### 后期可进一步部署nginx ###
