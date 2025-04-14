import os
import datetime
import json
from flask import g, jsonify, current_app
from app.models.token import Token
from app.models.task import Task
from app.utils.db import get_redis_client
from app.config import get_config
from app.utils.sync_manager import SyncManager, SYNC_STATUS_KEY
from app.models.user import User
import sys

config = get_config()
# 使用延迟加载方式，而不是在模块导入时获取Redis连接
_redis_client = None

def get_module_redis():
    """延迟加载Redis客户端，确保在应用上下文中使用"""
    global _redis_client
    if _redis_client is None:
        _redis_client = get_redis_client()
    return _redis_client

class SystemController:
    """系统控制器"""
    
    @staticmethod
    def get_system_info():
        """获取系统信息"""
        now = config.get_now()
        
        # 获取Redis客户端
        r = get_redis_client()
        
        # 记录程序启动时间
        from app import STARTUP_TIME

        # 同步Redis中的tokens数据
        tokens = Token.get_user_tokens(None)

        # 计算系统运行时间
        uptime = datetime.datetime.now() - STARTUP_TIME
        hours, remainder = divmod(uptime.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{int(hours)}小时{int(minutes)}分钟{int(seconds)}秒"

        # 获取内存任务数量
        memory_tasks_count = len(Task.tasks) if not r else 0
        
        # 获取用户数量
        total_users = 0
        if r:
            try:
                # 使用try/except来处理可能的键类型错误
                total_users = len(r.keys("user:*")) or 0
            except Exception as e:
                print(f"统计用户数量出错: {str(e)}")

        system_data = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "startup_time": STARTUP_TIME.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": uptime_str,
            "timezone": config.TIMEZONE,
            "redis_connected": r is not None,
            "redis_host": config.REDIS_HOST if r else "未连接",
            "redis_port": config.REDIS_PORT if r else "未连接",
            "redis_ssl": "已启用" if config.REDIS_SSL else "未启用",
            "total_tasks": len(Task.get_all_tasks()),
            "memory_tasks": memory_tasks_count,
            "total_tokens": len(tokens),
            "total_users": total_users,
            "python_version": sys.version.split()[0],
        }
        
        return system_data
    
    @staticmethod
    def get_user_info():
        """获取当前用户信息"""
        username = g.user_id
        is_admin = g.is_admin
        
        return {
            'username': username,
            'is_admin': is_admin
        }
    
    @staticmethod
    def get_sync_status():
        """获取数据同步状态"""
        # 获取Redis客户端
        redis = get_redis_client()
        if redis is None:
            return jsonify({
                "success": False,
                "message": "Redis连接失败，无法获取同步状态",
                "data": {
                    "last_sync_time": None,
                    "duration": None,
                    "success": None,
                    "error": "Redis连接失败",
                    "pending_sync": {
                        "users": 0,
                        "tokens": 0,
                        "tasks": 0
                    }
                }
            })
            
        # 获取同步状态
        status_json = redis.get(SYNC_STATUS_KEY)
        status = {}
        if status_json:
            try:
                status = json.loads(status_json)
            except:
                status = {}
                
        # 获取待同步数据数量
        pending_users = User.get_pending_sync_count(redis)
        pending_tokens = Token.get_pending_sync_count(redis)
        pending_tasks = Task.get_pending_sync_count(redis)
        
        return jsonify({
            "success": True,
            "message": "获取同步状态成功",
            "data": {
                "last_sync_time": status.get("last_sync_time"),
                "duration": status.get("duration"),
                "success": status.get("success"),
                "error": status.get("error"),
                "pending_sync": {
                    "users": pending_users,
                    "tokens": pending_tokens,
                    "tasks": pending_tasks
                }
            }
        })
        
    @staticmethod
    def trigger_sync():
        """手动触发数据同步"""
        try:
            SyncManager.run_sync()
            return jsonify({
                "success": True,
                "message": "数据同步已触发，正在同步中"
            })
        except Exception as e:
            return jsonify({
                "success": False,
                "message": f"触发同步失败: {str(e)}"
            }) 