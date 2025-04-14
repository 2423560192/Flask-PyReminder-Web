import datetime
import os
from flask import Flask, render_template, g
from flask_cors import CORS
from app.utils.db import setup_database, close_db_session
from app.controllers.task_controller import TaskChecker
from app.utils.sync_manager import SyncManager
from app.config import get_config

# 记录程序启动时间
STARTUP_TIME = datetime.datetime.now()

# 创建临时应用实例用于初始化数据库
temp_app = Flask(__name__)
temp_app.config.from_object(get_config())

# 初始化数据库连接池（在导入其他模块之前）
with temp_app.app_context():
    db_status = setup_database(temp_app)
    if not db_status['db_available']:
        print("警告: 数据库初始化失败，应用可能无法正常工作")
    if not db_status['redis_available']:
        print("警告: Redis初始化失败，应用可能无法正常工作")

# 现在可以安全地导入其他模块
from app.blueprints.main import main_bp
from app.blueprints.auth import auth_bp
from app.blueprints.token import token_bp
from app.blueprints.task import task_bp

def create_app():
    """创建Flask应用实例"""
    
    app = Flask(__name__)
    CORS(app)

    # 加载配置
    config = get_config()
    app.config.from_object(config)
    
    # 初始化应用状态
    app.config['TASK_CHECKER_STARTED'] = False
    app.config['SYNC_MANAGER_STARTED'] = False

    # 注册蓝图
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(token_bp)
    app.register_blueprint(task_bp)

    # 注册模板过滤器
    @app.template_filter('format_datetime')
    def format_datetime(dt):
        """自定义过滤器：格式化日期时间"""
        if isinstance(dt, datetime.datetime):
            return dt.strftime("%Y-%m-%d %H:%M")
        return dt

    # 初始化同步管理器和任务检查器（仅在主进程中执行，且确保只执行一次）
    if (not app.debug or (app.debug and os.environ.get('WERKZEUG_RUN_MAIN') == 'true')) and not app.config['TASK_CHECKER_STARTED'] and not app.config['SYNC_MANAGER_STARTED']:
        with app.app_context():
            # 初始化同步管理器
            sync_manager = SyncManager()
            sync_manager.start(app)
            app.config['SYNC_MANAGER_STARTED'] = True
            print("已启动同步管理器")

            # 启动任务检查线程
            TaskChecker.start_checker()
            app.config['TASK_CHECKER_STARTED'] = True
            print("已启动任务检查线程")
            
            print("时间提醒助手启动...")

    # 设置密钥
    app.secret_key = config.SECRET_KEY

    # 注册404处理器
    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('error.html', error="页面不存在", code=404), 404

    # 注册500处理器
    @app.errorhandler(500)
    def server_error(e):
        return render_template('error.html', error="服务器内部错误", code=500), 500

    # 注册应用关闭时的清理操作
    @app.teardown_appcontext
    def cleanup(exception=None):
        """清理函数，在请求结束时关闭数据库会话和Redis连接"""
        close_db_session()
        from app.utils.redis_helper import redis_helper
        redis_helper.close()

    return app


def register_blueprints(app):
    """注册所有蓝图"""
    from app.blueprints.main import main_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.task import task_bp
    from app.blueprints.token import token_bp
    from app.blueprints.system import system_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(task_bp)
    app.register_blueprint(token_bp)
    app.register_blueprint(system_bp)
