import os
import secrets
import hashlib
import pytz
import logging
from datetime import timedelta
from dotenv import load_dotenv

# 创建logger
logger = logging.getLogger('config')

# 加载环境变量
load_dotenv()


# 基础配置
class Config:
    """应用基础配置类"""
    # Flask配置
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'default_secret_key')
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', SECRET_KEY)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)  # 令牌有效期24小时

    # 管理员配置
    ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD_HASH = os.getenv('ADMIN_PASSWORD_HASH',
                                    hashlib.sha256('admin5201314'.encode()).hexdigest())

    # 时区设置
    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')  # 默认使用中国时区
    try:
        TZ = pytz.timezone(TIMEZONE)
    except Exception as e:
        print(f"时区设置错误，将使用UTC: {str(e)}")
        TZ = pytz.UTC

    # 数据库配置
    DATABASE_URL = os.getenv('DATABASE_URL',
                             'mysql+pymysql://root:5201314@localhost/pyreminder?charset=utf8mb4')

    # Redis配置
    REDIS_URL = os.getenv('REDIS_URL')
    REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
    REDIS_DB = 0  # Upstash只支持0号数据库
    REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')
    REDIS_SSL = os.getenv('REDIS_SSL', 'False').lower() in ['true', '1', 'yes']

    # 默认通知Token
    DEFAULT_NOTIFICATION_TOKEN = os.getenv('NOTIFICATION_TOKEN', 'XZ77c1d923959433459ec3a08556a6a5b6')

    # 键名配置
    TASK_ID_KEY = "task:id_counter"  # 任务ID计数器，用于生成自增的任务ID
    TASKS_KEY = "tasks"  # 存储所有任务的哈希表，key为任务ID，value为任务JSON数据
    TASKS_HASH_KEY = "tasks:hash"  # 备用任务哈希表，用于旧版本兼容
    USER_TASKS_KEY = "user:{username}:tasks"  # 用户任务关联集合，存储用户所拥有的任务ID，{username}会被替换为实际用户名
    PENDING_TASKS_KEY = "tasks:pending"  # 待处理任务有序集合，按触发时间排序，用于快速查找需要触发的任务
    TOKENS_KEY = "tokens"  # 存储所有通知令牌的哈希表，key为令牌名称，value为令牌数据
    USERS_KEY = "users"  # 存储所有用户信息的哈希表，key为用户名，value为用户数据
    USER_ID_COUNTER = "user:id_counter"  # 用户ID计数器，用于生成自增的用户ID

    # 本地文件路径（仅作为备用）
    TOKENS_FILE = 'tokens.yaml'

    # 调试设置
    DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1', 'yes']

    @staticmethod
    def get_now():
        """获取当前时间（带时区）"""
        import datetime
        return datetime.datetime.now(Config.TZ)


class DevelopmentConfig(Config):
    """开发环境配置"""
    DEBUG = True


class ProductionConfig(Config):
    """生产环境配置"""
    DEBUG = False


class TestingConfig(Config):
    """测试环境配置"""
    TESTING = True
    DEBUG = True


# 根据环境变量选择配置
config_dict = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}


def get_config():
    """获取当前环境配置"""
    config_name = os.getenv('FLASK_ENV', 'default')
    logger.info(f"加载配置: {config_name}")
    return config_dict.get(config_name, config_dict['default'])
