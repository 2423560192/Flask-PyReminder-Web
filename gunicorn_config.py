"""
Gunicorn配置文件
"""
import os
import multiprocessing

# 绑定的IP和端口
bind = "0.0.0.0:" + os.getenv("PORT", "5000")

# 工作进程数量
workers = int(os.getenv("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))

# 工作模式
worker_class = "sync"

# 超时时间
timeout = 120

# 日志级别
loglevel = "info"

# 自动重载
reload = os.getenv("FLASK_DEBUG", "False").lower() in ["true", "1", "yes"]

# 预加载应用
preload_app = True

# 应用的导入路径
wsgi_app = "run:app"

# 日志配置
accesslog = "-"
errorlog = "-"

# 进程名称
proc_name = "pyreminder" 