import redis
from flask import g, current_app
from app.config import get_config

config = get_config()

class RedisHelper:
    def __init__(self):
        """初始化Redis连接池"""
        self.pool = None
        self._init_pool()

    def _init_pool(self):
        """初始化Redis连接池"""
        if self.pool is None:
            if config.REDIS_URL:
                self.pool = redis.ConnectionPool.from_url(
                    config.REDIS_URL,
                    decode_responses=True
                )
            else:
                self.pool = redis.ConnectionPool(
                    host=config.REDIS_HOST,
                    port=config.REDIS_PORT,
                    db=config.REDIS_DB,
                    password=config.REDIS_PASSWORD,
                    decode_responses=True
                )
            # 测试连接
            test_client = redis.Redis(connection_pool=self.pool)
            test_client.ping()
            print(f"Redis连接池创建成功 (数据库: {config.REDIS_DB})")

    def get_client(self):
        """获取当前请求上下文中Redis客户端"""
        if 'redis_client' not in g:
            g.redis_client = redis.Redis(connection_pool=self.pool)
        return g.redis_client

    def close(self):
        """清理Redis客户端连接"""
        client = g.pop('redis_client', None)
        if client:
            client.close()

# 创建全局RedisHelper实例
redis_helper = RedisHelper() 