import json
import datetime
import urllib.parse
import redis
from redis.connection import ConnectionPool
import yaml
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from flask import current_app, g
from app.config import get_config

# 导入放在顶部，避免循环导入
from app.utils.redis_helper import redis_helper

config = get_config()

# SQLAlchemy基类
Base = declarative_base()

# 数据库和Redis的全局标志
db_available = False
redis_available = False

def register_engine_events(engine):
    """注册数据库引擎事件监听器"""
    def _engine_connect(conn, branch):
        # 连接时检查健康状态
        global db_available
        try:
            conn.scalar("SELECT 1")
            db_available = True
        except Exception as e:
            db_available = False
            print(f"数据库连接检查失败: {str(e)}")
    
    # 使用event.listen注册事件监听器
    event.listen(engine, 'engine_connect', _engine_connect)

def setup_database(app):
    """设置并初始化数据库连接"""
    global db_available
    db_available = False
    # 尝试连接数据库
    try:
        DATABASE_URL = config.DATABASE_URL
        app.logger.info(f"正在尝试连接数据库: {DATABASE_URL}")
        
        # 创建数据库引擎
        engine = create_engine(DATABASE_URL)
        
        # 注册引擎事件监听器
        register_engine_events(engine)
        
        # 创建会话工厂
        session_factory = sessionmaker(bind=engine)
        
        # 创建线程安全的会话
        db_session = scoped_session(session_factory)
        
        # 将会话绑定到模型的元数据
        Base.query = db_session.query_property()
        
        # 检查连接
        with engine.connect() as conn:
            conn.execute("SELECT 1")
            db_available = True
            app.logger.info("数据库连接成功")
    except Exception as e:
        db_available = False
        app.logger.error(f"数据库连接失败: {str(e)}")
    
    return db_available

def get_redis_client():
    """获取Redis客户端实例"""
    return redis_helper.get_client()

def get_db_session():
    """获取数据库会话，从连接池中获取"""
    try:
        # 如果已经在g对象中存在数据库会话，直接返回
        if current_app and hasattr(g, 'db_session'):
            return g.db_session
            
        # 如果全局数据库引擎不存在，创建一个
        if current_app and hasattr(g, 'db_engine'):
            engine = g.db_engine
        else:
            engine = create_engine(
                config.DATABASE_URL,
                pool_size=5,
                max_overflow=10,
                pool_timeout=30,
                pool_recycle=1800,
                pool_pre_ping=True
            )
        
        # 创建新的数据库会话
        session_factory = sessionmaker(bind=engine)
        session = session_factory()
        
        # 如果在应用上下文中，保存到g对象
        if current_app:
            g.db_session = session
            g.db_engine = engine
            
        return session
            
    except Exception as e:
        print(f"创建数据库会话失败: {str(e)}")
        return None

def close_db_session(session=None):
    """关闭数据库会话，将连接返回到连接池"""
    try:
        # 如果提供了特定的会话，直接关闭它
        if session:
            session.close()
            return
            
        # 检查是否在应用上下文中
        try:
            from flask import g, has_app_context
            if has_app_context() and hasattr(g, 'db_session'):
                g.db_session.close()
                g.pop('db_session', None)
        except (ImportError, RuntimeError) as e:
            # 不在应用上下文中或导入失败，记录信息并继续
            print(f"在应用上下文外尝试关闭会话: {str(e)}")
            # 这里不抛出异常，允许程序继续运行
    except Exception as e:
        print(f"关闭数据库会话时出错: {str(e)}")

def close_redis_client(e=None):
    """关闭Redis客户端连接"""
    try:
        # 检查是否在应用上下文中
        from flask import has_app_context
        if not has_app_context():
            print("在应用上下文外尝试关闭Redis连接")
            return
    except (ImportError, RuntimeError):
        # 不在应用上下文中，忽略
        return
        
    # 在应用上下文中，正常关闭
    redis_helper.close()

# 添加数据库事件监听器
def ping_connection(connection, branch):
    """在每次使用连接前检查连接是否有效"""
    if branch:
        return

    try:
        connection.scalar("SELECT 1")
    except Exception:
        # 如果连接无效，关闭它并让连接池创建新的
        try:
            connection.close()
        except Exception as e:
            print(f"关闭无效连接时出错: {str(e)}")
        raise

# 使用event.listen而不是装饰器注册事件
def register_engine_events(engine):
    """注册数据库引擎的事件监听器"""
    from sqlalchemy import event
    
    # 检查连接是否有效的函数
    def ping_connection(connection, connection_record, connection_proxy):
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1")
        except:
            # 如果执行失败，抛出异常以标记连接为无效
            raise Exception("数据库连接已断开")
        cursor.close()
    
    # 使用event.listen注册事件
    event.listen(engine, 'checkout', ping_connection)

def json_serial(obj):
    """处理不可序列化的对象（如datetime）"""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError(f"类型{type(obj)}不可序列化")

def create_tables(engine):
    """创建所有数据表"""
    if current_app.config.get('DB_AVAILABLE'):
        Base.metadata.create_all(current_app.config['SQLALCHEMY_ENGINE'])
        print("已创建所有数据表")
    else:
        print("数据库不可用，无法创建表")

def recreate_all_tables():
    """重新创建所有数据库表"""
    global current_app
    
    try:
        if current_app.config.get('SQLALCHEMY_ENGINE') is None:
            print("数据库引擎未初始化，尝试重新初始化...")
            current_app.config['SQLALCHEMY_ENGINE'] = create_engine(
                config.DATABASE_URL,
                poolclass=QueuePool,
                pool_size=5,  # 连接池大小
                max_overflow=10,  # 最大额外连接数
                pool_timeout=30,  # 连接超时时间
                pool_recycle=1800,  # 30分钟后回收连接
                pool_pre_ping=True,  # 启用连接检查
                echo=False  # 禁用SQL语句日志
            )
            
        if current_app.config.get('SQLALCHEMY_ENGINE') is None:
            print("无法初始化数据库引擎，跳过表创建")
            return
            
        # 在事务中执行表的重建
        with current_app.config['SQLALCHEMY_ENGINE'].begin() as connection:
            # 删除所有现有表
            Base.metadata.drop_all(connection)
            print("已删除所有现有表")
            
            # 创建所有表
            Base.metadata.create_all(connection)
            print("已创建所有数据表")
        
    except Exception as e:
        print(f"重新创建数据表失败: {str(e)}")
        print(f"错误类型: {type(e).__name__}")
        print(f"错误详情: {str(e)}")
        print("\n请检查以下内容:")
        print("1. 确保数据库连接正常")
        print("2. 确保有足够的权限创建和删除表")
        print("3. 确保没有其他连接正在使用这些表") 