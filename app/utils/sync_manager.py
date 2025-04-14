import os
import json
import time
import threading
import datetime
import logging
from flask import current_app
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
            self.app = None
    
    def start(self, app=None):
        """启动同步线程"""
        # 保存应用实例，用于创建应用上下文
        if app is not None:
            self.app = app
        
        if self.sync_thread is None or not self.sync_thread.is_alive():
            self.running = True
            self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self.sync_thread.start()
            logger.info(f"同步调度器已启动，同步间隔: {self.sync_interval}秒")
            return True
        return False
    
    def stop(self):
        """停止同步线程"""
        self.running = False
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=1)
            self.sync_thread = None
            logger.info("同步调度器已停止")
            return True
        return False
    
    def is_running(self):
        """检查同步线程是否在运行"""
        return self.running and self.sync_thread is not None and self.sync_thread.is_alive()
    
    def _sync_loop(self):
        """同步循环"""
        while self.running:
            try:
                # 使用应用上下文
                if self.app:
                    with self.app.app_context():
                        with self.sync_lock:
                            self._sync_all()
                else:
                    from flask import current_app
                    # 尝试使用当前应用
                    if current_app:
                        with self.sync_lock:
                            self._sync_all()
                    else:
                        logger.error("无法获取应用上下文，同步无法运行")
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
                
                # 获取上次同步时间戳和本次同步时间戳
                last_sync_key = "last_full_sync_timestamp"
                last_sync_time = redis.get(last_sync_key)
                last_sync_timestamp = float(last_sync_time) if last_sync_time else 0
                
                # 当前时间戳作为本次同步时间戳
                current_timestamp = time.time()
                
                # 检查是否需要进行全量同步
                force_full_sync = False
                last_full_sync_key = "last_full_sync_date"
                last_full_sync_date = redis.get(last_full_sync_key)
                
                # 如果没有记录上次全量同步日期，或者距离上次全量同步已超过24小时，则进行全量同步
                if not last_full_sync_date:
                    force_full_sync = True
                else:
                    last_date = datetime.datetime.fromisoformat(last_full_sync_date.decode('utf-8') if isinstance(last_full_sync_date, bytes) else last_full_sync_date)
                    today = datetime.datetime.now()
                    if (today - last_date).days >= 1:
                        force_full_sync = True
                
                try:
                    # 记录同步开始时间
                    sync_start_time = time.time()
                    
                    # 设置同步参数
                    sync_params = {
                        "last_sync_timestamp": last_sync_timestamp,
                        "current_timestamp": current_timestamp,
                        "force_full_sync": force_full_sync
                    }
                    
                    # 同步用户数据
                    logger.info("开始同步用户数据...")
                    user_result = User.sync_to_postgres()
                    
                    # 同步令牌数据
                    logger.info("开始同步令牌数据...")
                    token_result = Token.sync_to_postgres()
                    
                    # 同步任务数据
                    logger.info("开始同步任务数据...")
                    task_result = Task.sync_to_postgres()
                    
                    # 记录同步完成时间
                    sync_end_time = time.time()
                    sync_duration = sync_end_time - sync_start_time
                    
                    # 如果是全量同步，更新全量同步日期
                    if force_full_sync:
                        redis.set(last_full_sync_key, datetime.datetime.now().isoformat())
                    
                    # 更新上次同步时间戳
                    redis.set(last_sync_key, current_timestamp)
                    
                    logger.info(f"所有数据同步完成，耗时: {sync_duration:.2f}秒")
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
    
    def set_sync_interval(self, seconds):
        """设置同步间隔"""
        if seconds > 0:
            self.sync_interval = seconds
            logger.info(f"同步间隔已更新为: {seconds}秒")
            return True
        return False
    
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


# 获取同步管理器实例
def get_sync_manager():
    """获取同步管理器的单例实例"""
    return SyncManager()
