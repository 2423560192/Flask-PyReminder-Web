import datetime
import logging
import os
import threading
from flask import Flask, render_template, g
from flask_cors import CORS
from app.utils.db import setup_database, close_db_session
from app.config import get_config

# 记录程序启动时间
STARTUP_TIME = datetime.datetime.now()

# 保存全局应用实例引用
flask_app = None
sync_manager = None
task_checker = None
startup_lock = threading.Lock()

def create_app():
    """创建Flask应用实例"""
    global flask_app, sync_manager, task_checker
    
    # 如果应用已经创建，直接返回
    if flask_app is not None:
        return flask_app
        
    # 创建新的应用实例
    app = Flask(__name__)
    CORS(app)

    # 加载配置
    config = get_config()
    app.config.from_object(config)
    
    # 初始化应用状态
    app.config['TASK_CHECKER_STARTED'] = False
    app.config['SYNC_MANAGER_STARTED'] = False
    app.config['ADMIN_CHECKED'] = False
    app.config['DB_AVAILABLE'] = False
    app.config['REDIS_AVAILABLE'] = False

    # 注册蓝图
    from app.blueprints.main import main_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.token import token_bp
    from app.blueprints.task import task_bp
    from app.blueprints.system import system_bp
    
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(token_bp)
    app.register_blueprint(task_bp)
    app.register_blueprint(system_bp)

    # 注册模板过滤器
    @app.template_filter('format_datetime')
    def format_datetime(dt):
        """自定义过滤器：格式化日期时间"""
        if isinstance(dt, datetime.datetime):
            return dt.strftime("%Y-%m-%d %H:%M")
        return dt

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
        try:
            from app.utils.db import close_db_session
            close_db_session()
        except Exception as e:
            app.logger.error(f"关闭数据库会话时出错: {str(e)}")
            
        try:
            from app.utils.redis_helper import redis_helper
            redis_helper.close()
        except Exception as e:
            app.logger.error(f"关闭Redis连接时出错: {str(e)}")
        
        if exception:
            app.logger.error(f"请求处理过程中出现异常: {str(exception)}")
    
    # 保存全局引用
    flask_app = app
    
    # 初始化数据库 - 确保在应用上下文中进行
    with app.app_context():
        # 初始化数据库连接
        db_status = setup_database(app)
        app.config['DB_AVAILABLE'] = db_status
        
        # 初始化Redis连接
        from app.utils.redis_helper import redis_helper
        redis_available = redis_helper.check_connection()
        app.config['REDIS_AVAILABLE'] = redis_available
        
        if not db_status:
            print("警告: 数据库初始化失败，应用可能无法正常工作")
        if not redis_available:
            print("警告: Redis初始化失败，应用可能无法正常工作")
        # 初始化后台服务 - 仅在主进程中执行一次

        if (not app.debug or (app.debug and os.environ.get('WERKZEUG_RUN_MAIN') == 'true')):
            # 使用锁确保线程安全
            with startup_lock:
                if not app.config['ADMIN_CHECKED']:
                    # 确保至少有一个管理员账户
                    from app.models.user import User
                    User.ensure_admin()
                    app.config['ADMIN_CHECKED'] = True
                    print("已完成管理员账户检查")
                
                if not app.config['SYNC_MANAGER_STARTED']:
                    # 初始化同步管理器
                    from app.utils.sync_manager import SyncManager
                    sync_manager = SyncManager()
                    sync_manager.start(app)
                    app.config['SYNC_MANAGER_STARTED'] = True
                    print("已启动同步管理器")

                if not app.config['TASK_CHECKER_STARTED']:
                    # 启动任务检查线程
                    from app.controllers.task_controller import TaskChecker
                    task_checker = TaskChecker
                    task_checker.start_checker()
                    app.config['TASK_CHECKER_STARTED'] = True
                    print("已启动任务检查线程")
                
                print("时间提醒助手启动...")

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
