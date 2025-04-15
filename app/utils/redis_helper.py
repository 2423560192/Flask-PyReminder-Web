import redis
import traceback
from flask import g, current_app, has_app_context, has_request_context
from app.config import get_config
import os

config = get_config()

class RedisHelper:
    def __init__(self):
        """初始化Redis连接池"""
        self.pool = None
        self._standalone_client = None
        # 延迟初始化连接池，但在开发环境中尝试提前初始化以捕获错误
        try:
            self._init_pool()
            print("Redis连接池预初始化成功")
        except Exception as e:
            print(f"Redis连接池预初始化失败（这是正常的，将在后续请求中重试）: {str(e)}")

    def _init_pool(self):
        """初始化Redis连接池 - 延迟加载逻辑"""
        if self.pool is not None:
            return  # 已初始化，跳过
            
        try:
            # 检查环境变量中是否有完整的Redis URL（Render环境中可能作为单一变量提供）
            redis_url = os.environ.get('REDIS_URL', config.REDIS_URL)
            redis_ssl = os.environ.get('REDIS_SSL', config.REDIS_SSL)
            redis_ssl = redis_ssl if isinstance(redis_ssl, bool) else redis_ssl.lower() in ['true', '1', 'yes']
            
            print(f"正在初始化Redis连接池... URL配置: {bool(redis_url)}, 主机: {config.REDIS_HOST}, 端口: {config.REDIS_PORT}, SSL: {redis_ssl}")
            
            # 检查Redis版本对SSL的支持
            redis_version = getattr(redis, '__version__', '0.0.0')
            supports_ssl = True
            
            try:
                # 尝试导入Connection类来检查是否支持SSL
                from redis.connection import Connection
                # 检查Connection类的__init__方法是否接受ssl参数
                import inspect
                conn_params = inspect.signature(Connection.__init__).parameters
                supports_ssl = 'ssl' in conn_params
                print(f"Redis版本: {redis_version}, 支持SSL: {supports_ssl}")
            except (ImportError, AttributeError):
                # 如果导入失败，假设不支持SSL
                supports_ssl = False
                print(f"无法确定Redis客户端版本，假设不支持SSL")
            
            # 准备连接参数
            connection_kwargs = {
                'decode_responses': True
            }
            
            # 仅当支持SSL且需要SSL时添加SSL参数
            if supports_ssl and redis_ssl:
                connection_kwargs.update({
                    'ssl': True,
                    'ssl_cert_reqs': None  # 不验证SSL证书
                })
            elif redis_ssl and not supports_ssl:
                print("警告: Redis客户端不支持SSL，但配置要求SSL连接。连接可能会失败。")
                
            if redis_url:
                print(f"使用URL初始化Redis连接池: {redis_url[:20]}...")  # 只打印URL前20个字符，避免泄露密码
                # 如果使用URL，我们需要更特殊的处理
                if redis_ssl and not supports_ssl:
                    # 对于旧版本，我们可能需要修改URL来添加rediss://前缀
                    if redis_url.startswith('redis://'):
                        redis_url = redis_url.replace('redis://', 'rediss://')
                        print(f"已修改URL使用rediss://前缀支持SSL")
                
                self.pool = redis.ConnectionPool.from_url(
                    redis_url,
                    **connection_kwargs
                )
            else:
                print(f"使用主机和端口初始化Redis连接池: {config.REDIS_HOST}:{config.REDIS_PORT}")
                connection_kwargs.update({
                    'host': config.REDIS_HOST,
                    'port': config.REDIS_PORT,
                    'db': config.REDIS_DB,
                    'password': config.REDIS_PASSWORD
                })
                
                self.pool = redis.ConnectionPool(
                    **connection_kwargs
                )
                
            # 测试连接
            test_client = redis.Redis(connection_pool=self.pool)
            test_client.ping()
            print(f"Redis连接池创建成功 (数据库: {config.REDIS_DB})")
            
            # 打印使用的键名
            print(f"使用的Redis键名: TASKS_KEY={config.TASKS_KEY}, TASK_ID_KEY={config.TASK_ID_KEY}")
            print(f"用户任务键名: USER_TASKS_KEY={config.USER_TASKS_KEY}, PENDING_TASKS_KEY={config.PENDING_TASKS_KEY}")
        except Exception as e:
            print(f"Redis连接池创建失败: {str(e)}")
            print("Redis错误详情:")
            traceback.print_exc()
            self.pool = None  # 确保连接池为None

    def get_standalone_client(self):
        """获取独立的Redis客户端，不依赖于Flask上下文
        在应用上下文外部使用此方法，如启动时和后台任务中
        """
        # 确保连接池已初始化
        if self.pool is None:
            try:
                self._init_pool()
            except Exception as e:
                print(f"获取独立Redis客户端时初始化连接池失败: {str(e)}")
                return None
            
        if self.pool is None:
            return None  # 连接失败
            
        # 延迟创建并缓存客户端
        try:
            if self._standalone_client is None:
                self._standalone_client = redis.Redis(connection_pool=self.pool)
            return self._standalone_client
        except Exception as e:
            print(f"创建独立Redis客户端失败: {str(e)}")
            return None

    def get_client(self):
        """获取Redis客户端
        优先使用请求上下文，否则返回独立客户端
        """
        # 确保连接池已初始化
        if self.pool is None:
            try:
                self._init_pool()
            except Exception as e:
                print(f"获取Redis客户端时初始化连接池失败: {str(e)}")
                return None
            
        if self.pool is None:
            return None  # 连接失败
            
        # 检查是否在请求上下文中
        try:
            if has_request_context():
                # 使用请求上下文
                if 'redis_client' not in g:
                    g.redis_client = redis.Redis(connection_pool=self.pool)
                return g.redis_client
        except Exception as e:
            print(f"使用请求上下文获取Redis客户端失败: {str(e)}")
            # 上下文检查出错，返回独立客户端
            pass
            
        # 不在请求上下文中或无法访问g，返回独立客户端
        return self.get_standalone_client()

    def close(self):
        """清理Redis客户端连接"""
        # 只在请求上下文中尝试关闭连接
        try:
            if has_request_context() and hasattr(g, 'redis_client'):
                client = g.pop('redis_client', None)
                if client:
                    client.close()
        except Exception:
            # 上下文检查出错，忽略
            pass

# 创建全局RedisHelper实例
redis_helper = RedisHelper()
# 确保应用初始化时不会触发连接 