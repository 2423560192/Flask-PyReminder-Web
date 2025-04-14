import os
import json
import time
import threading
import datetime
import logging
from app.utils.db import get_db_session, close_db_session
from app.utils.redis_helper import redis_helper
from app.utils.constants import OP_TYPES, KEY_PREFIX, SYNC_LOCK_KEY, SYNC_STATUS_KEY
from app.config import get_config
from app.models.user import User
from app.models.token import Token
from app.models.task import Task

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger('SyncManager')
config = get_config()


class SyncManager:
    """数据同步管理器"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, 'initialized'):
            self.initialized = True
            self.sync_thread = None
            self.running = False
            self.sync_interval = 300  # 同步间隔（秒）
            self.sync_lock = threading.Lock()
    
    def start(self):
        """启动同步线程"""
        if self.sync_thread is None or not self.sync_thread.is_alive():
            self.running = True
            self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self.sync_thread.start()
            logger.info(f"同步调度器已启动，同步间隔: {self.sync_interval}秒")
    
    def stop(self):
        """停止同步线程"""
        self.running = False
        if self.sync_thread:
            self.sync_thread.join()
            self.sync_thread = None
            logger.info("同步调度器已停止")
    
    def _sync_loop(self):
        """同步循环"""
        while self.running:
            try:
                with self.sync_lock:
                    self._sync_all()
            except Exception as e:
                logger.error(f"同步过程中出错: {str(e)}")
            time.sleep(self.sync_interval)
    
    def _sync_all(self):
        """同步所有数据"""
        try:
            # 获取Redis连接
            redis = redis_helper.get_client()
            if not redis:
                logger.warning("无法获取Redis连接，等待重试...")
                return

            # 获取数据库会话
            session = get_db_session()
            if not session:
                logger.warning("无法获取数据库会话，等待重试...")
                return

            try:
                # 设置同步状态
                redis.set(SYNC_STATUS_KEY, "running", ex=600)  # 10分钟过期
                
                # 获取同步锁
                if not redis.set(SYNC_LOCK_KEY, "1", ex=600, nx=True):
                    logger.info("另一个同步进程正在运行，跳过本次同步")
                    return

                try:
                    # 同步用户数据
                    logger.info("开始同步用户数据...")
                    User.sync_to_postgres(redis, session)
                    
                    # 同步令牌数据
                    logger.info("开始同步令牌数据...")
                    Token.sync_to_postgres(redis, session)
                    
                    # 同步任务数据
                    logger.info("开始同步任务数据...")
                    Task.sync_to_postgres(redis, session)
                    
                    logger.info("所有数据同步完成")
                finally:
                    # 释放同步锁
                    redis.delete(SYNC_LOCK_KEY)
                    redis.set(SYNC_STATUS_KEY, "completed", ex=600)

            except Exception as e:
                logger.error(f"同步过程中出错: {str(e)}")
                redis.set(SYNC_STATUS_KEY, f"error: {str(e)}", ex=600)
                session.rollback()
            finally:
                close_db_session(session)

        except Exception as e:
            logger.error(f"同步过程中出错: {str(e)}")
            try:
                redis = redis_helper.get_client()
                if redis:
                    redis.set(SYNC_STATUS_KEY, f"error: {str(e)}", ex=600)
            except:
                pass
    
    @classmethod
    def add_to_sync_queue(cls, model_name, key, data, operation):
        """添加到同步队列"""
        try:
            redis = redis_helper.get_client()
            if redis:
                queue_key = f"sync_queue:{model_name}"
                item = {
                    'key': key,
                    'data': data,
                    'operation': operation,
                    'timestamp': time.time()
                }
                redis.rpush(queue_key, json.dumps(item))
                logger.info(f"已添加到同步队列: {model_name} - {key}")
        except Exception as e:
            logger.error(f"添加到同步队列失败: {str(e)}")
            
    @classmethod
    def get_sync_status(cls):
        """获取同步状态"""
        try:
            redis = redis_helper.get_client()
            if redis:
                status = redis.get(SYNC_STATUS_KEY)
                return status.decode('utf-8') if status else "unknown"
        except Exception as e:
            logger.error(f"获取同步状态失败: {str(e)}")
            return "error"


class SyncScheduler:
    """数据同步调度器，定时运行同步任务"""

    def __init__(self, interval=300):  # 默认5分钟同步一次
        self.interval = interval
        self.running = False
        self.thread = None

    def start(self):
        """启动同步调度器"""
        if self.running:
            logger.warning('同步调度器已在运行中')
            return

        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True  # 设置为守护线程，主程序退出时，线程自动结束
        self.thread.start()
        logger.info(f'同步调度器已启动，同步间隔: {self.interval}秒')

    def stop(self):
        """停止同步调度器"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
        logger.info('同步调度器已停止')

    def _run(self):
        """定时同步任务的线程函数"""
        while self.running:
            try:
                # 运行同步
                SyncManager.run_sync()
            except Exception as e:
                logger.error(f'同步调度器运行出错: {str(e)}')

            # 等待下一次同步
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)


# 创建一个全局的同步调度器实例
sync_scheduler = SyncScheduler()


def start_sync_scheduler():
    """启动同步调度器"""
    sync_scheduler.start()


def stop_sync_scheduler():
    """停止同步调度器"""
    sync_scheduler.stop()
