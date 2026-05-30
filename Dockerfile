# 1. 选用精简版的 Python 3.12 作为基础环境镜像
FROM python:3.12-slim

# 2. 设置容器内部的工作目录
WORKDIR /app

# 3. 设置环境变量，防止 Python 产生 pyc 缓存文件，并让日志实时输出
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 4. 先把依赖文件复制进去（利用 Docker 缓存机制加速后续构建）
# 提示：记得在本地终端运行 pip freeze > requirements.txt 生成依赖清单
COPY requirements.txt /app/

# 5. 清洗并安装 Python 依赖（使用清华大学镜像源加速服务器构建）
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 6. 将本地当前目录下的所有代码及模板无损复制到容器的 /app 目录中
COPY . /app/

# 7. 暴露出 Flask 默认的 5000 端口
EXPOSE 5000

# 8. 生产环境启动命令：使用 gunicorn 承载 Flask 应用
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
