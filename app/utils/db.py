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
from app.utils import redis_helper

config = get_config()

# SQLAlchemy基类
Base = declarative_base()

def setup_database(app):
    """设置并初始化数据库连接"""
    try:
        print("开始初始化数据库连接...")
        # 创建数据库引擎
        engine = create_engine(
            config.DATABASE_URL,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True
        )
        
        # 测试连接
        with engine.connect() as connection:
            if 'mysql' in config.DATABASE_URL:
                result = connection.execute("SELECT VERSION()")
                version = result.scalar()
                print(f"数据库连接成功，MySQL版本: {version}")
            else:
                result = connection.execute("SELECT version()")
                version = result.scalar()
                print(f"数据库连接成功，PostgreSQL版本: {version}")
        
        # 创建会话工厂
        session_factory = sessionmaker(bind=engine)
        Session = scoped_session(session_factory)
        
        # 将引擎和会话工厂存储在应用配置中
        app.config['SQLALCHEMY_ENGINE'] = engine
        app.config['SQLALCHEMY_SESSION'] = Session
        app.config['DB_AVAILABLE'] = True
        
        print("数据库初始化完成")
        
        # 创建所有表（如果不存在）
        inspector = inspect(engine)
        if not inspector.has_table('users'):
            print("首次启动，创建所有数据表...")
            Base.metadata.create_all(engine)
            print("数据表创建完成")
        else:
            print("数据表已存在，跳过创建")
        
        # 测试Redis连接
        try:
            redis_client = get_redis_client()
            redis_client.ping()
            app.config['REDIS_AVAILABLE'] = True
            print("Redis连接成功")
        except Exception as e:
            app.config['REDIS_AVAILABLE'] = False
            print(f"警告: Redis连接失败: {str(e)}")
        
        return {
            'db_available': app.config['DB_AVAILABLE'],
            'redis_available': app.config['REDIS_AVAILABLE']
        }
        
    except Exception as e:
        print(f"警告: 数据库初始化失败!")
        print(f"错误类型: {type(e).__name__}")
        print(f"错误详情: {str(e)}")
        app.config['DB_AVAILABLE'] = False
        app.config['REDIS_AVAILABLE'] = False
        return {
            'db_available': False,
            'redis_available': False
        }

def get_redis_client():
    """从连接池获取Redis客户端，安全地处理上下文"""
    try:
        from app.utils.redis_helper import redis_helper
        return redis_helper.get_client()
    except Exception as e:
        print(f"获取Redis客户端出错: {str(e)}")
        return None

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
        if session:
            session.close()
        elif 'db_session' in g:
            g.db_session.close()
            g.pop('db_session', None)
    except Exception as e:
        print(f"关闭数据库会话时出错: {str(e)}")

def close_redis_client(e=None):
    """关闭Redis客户端连接"""
    redis_helper.close()

# 添加数据库事件监听器
@event.listens_for(Engine, "engine_connect")
def ping_connection(connection, branch):
    """在每次使用连接前检查连接是否有效"""
    if branch:
        return

    try:
        connection.scalar("SELECT 1")
    except Exception:
        # 如果连接无效，关闭它并让连接池创建新的
        connection.close()
        raise

def json_serial(obj):
    """处理不可序列化的对象（如datetime）"""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError(f"类型{type(obj)}不可序列化")

def create_tables():
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