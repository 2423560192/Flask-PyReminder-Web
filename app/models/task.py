import json
import datetime
import os
import yaml
from sqlalchemy import Column, Integer, String, Boolean, Text
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from app.utils.db import Base, get_db_session, close_db_session
from app.utils.constants import OP_TYPES, TASKS_KEY, PENDING_TASKS_KEY
from app.utils.helpers import parse_datetime
from app.config import get_config
from app.utils.redis_helper import redis_helper
from flask import current_app
import uuid
import re
import time

config = get_config()

# 自定义JSON序列化函数，用于处理datetime类型
def json_serial(obj):
    """JSON序列化函数，支持datetime类型"""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError(f"无法序列化类型 {type(obj)}")

# 内存存储模式的任务列表（仅当Redis不可用时使用）
tasks = []

# 从配置获取Redis键名
TASK_ID_KEY = config.TASK_ID_KEY   # 任务ID计数器
USER_TASKS_KEY = "user_{username}_tasks"  # 存储用户任务ID的Redis集合键名

# 打印使用的键名
print(f"使用的Redis键名: TASKS_KEY={TASKS_KEY}, TASK_ID_KEY={TASK_ID_KEY}")
print(f"用户任务键名: USER_TASKS_KEY={USER_TASKS_KEY}, PENDING_TASKS_KEY={PENDING_TASKS_KEY}")

class Task(Base):
    """任务模型"""
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=True)
    datetime = Column(DateTime(timezone=True), nullable=False)
    token_name = Column(String(255), nullable=False, default='默认')
    owner = Column(String(255), nullable=False)
    triggered = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f'<Task {self.title}>'
    
    @classmethod
    def get_next_task_id(cls):
        """获取下一个任务ID"""
        # 1. 获取Redis客户端
        r = redis_helper.get_client()
        
        if r:
            # 使用Redis的INCR命令自动递增
            return r.incr(TASK_ID_KEY)
        
        # 2. 尝试获取数据库会话
        session = get_db_session()
        if session:
            try:
                # 使用最新ID（如果有任务的话）
                latest_task = session.query(cls).order_by(cls.id.desc()).first()
                if latest_task:
                    return latest_task.id + 1
                return 1  # 如果没有任务，从1开始
            except Exception as e:
                print(f"从数据库获取最新任务ID失败: {str(e)}")
            finally:
                close_db_session(session)
        
        # 3. 从内存任务列表中找出最大ID
        max_id = 0
        for task in tasks:
            if task["id"] > max_id:
                max_id = task["id"]
        return max_id + 1
    
    @classmethod
    def save_task(cls, task_id, title, content, datetime_str, token_name, username=None):
        """保存任务到Redis或数据库"""
        # 解析日期时间
        task_datetime = parse_datetime(datetime_str)
        
        # 当前时间戳
        current_timestamp = time.time()
        
        # 构建任务数据
        task = {
            "id": task_id,
            "title": title,
            "content": content,
            "datetime": task_datetime.isoformat() if task_datetime else None,
            "token_name": token_name,
            "owner": username,
            "triggered": False,
            "created_at": datetime.datetime.now(config.TZ).isoformat(),
            "updated_at": current_timestamp
        }
        
        # 1. 获取Redis客户端并尝试保存
        r = redis_helper.get_client()
        
        if r:
            # 保存到Redis
            # 将任务保存为JSON字符串
            r.hset(TASKS_KEY, task_id, json.dumps(task, default=json_serial))
            
            # A. 添加到用户的任务集合
            if username:
                user_tasks_key = USER_TASKS_KEY.format(username=username)
                r.sadd(user_tasks_key, task_id)
            
            # B. 将未触发的任务添加到待处理队列
            if not task.get("triggered", False):
                # 计算任务触发时间的时间戳
                task_timestamp = task_datetime.timestamp() if task_datetime else 0
                # 添加到有序集合，分数为触发时间的时间戳
                r.zadd(PENDING_TASKS_KEY, {str(task_id): task_timestamp})
            
            # C. 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tasks',
                task_id,
                task,
                OP_TYPES['CREATE'] if task_id not in r.hkeys(TASKS_KEY) else OP_TYPES['UPDATE']
            )
            
            print(f"已保存任务 {task_id} 到Redis，键名: {TASKS_KEY}")
            return True
            
        # 2. 尝试获取数据库会话并保存
        db_session = get_db_session()
        if db_session:
            # 直接保存到数据库
            try:
                # 解析任务时间
                task_datetime_obj = task_datetime
                
                # 检查任务是否已存在
                existing_task = db_session.query(cls).filter(cls.id == task_id).first()

                if existing_task:
                    # 更新已存在的任务
                    existing_task.title = title
                    existing_task.content = content
                    existing_task.datetime = task_datetime_obj
                    existing_task.token_name = token_name
                    existing_task.owner = username
                    existing_task.triggered = False
                else:
                    # 创建新任务
                    new_task = cls(
                        id=task_id,
                        title=title,
                        content=content,
                        datetime=task_datetime_obj,
                        token_name=token_name,
                        owner=username,
                        triggered=False,
                        created_at=datetime.datetime.now(config.TZ)
                    )
                    db_session.add(new_task)

                db_session.commit()
                print(f"已保存任务 {task_id} 到数据库")
                return True
            except Exception as e:
                db_session.rollback()
                print(f"保存任务到数据库时出错: {str(e)}")
                return False
            finally:
                close_db_session(db_session)
        
        # 3. 如果上述方法都失败，保存到内存
        # 检查任务是否已存在
        for i, existing_task in enumerate(tasks):
            if existing_task["id"] == task_id:
                # 更新已存在的任务
                tasks[i] = task
                break
        else:
            # 添加新任务
            tasks.append(task)
        
        print(f"已保存任务 {task_id} 到内存")
        return True
    
    @classmethod
    def get_tasks(cls, username=None, include_triggered=True):
        """获取任务列表"""
        # 1. 首先尝试从Redis获取
        r = redis_helper.get_client()
        
        if r:
            try:
                # 从Redis加载所有任务
                all_tasks = r.hgetall(TASKS_KEY)
                
                if all_tasks:
                    tasks_list = []
                    for task_id, task_json in all_tasks.items():
                        try:
                            task = json.loads(task_json)
                            
                            # 如果指定了用户名，只返回该用户的任务
                            if username and task.get("owner") != username:
                                continue
                                
                            # 如果不包括已触发任务，跳过已触发的任务
                            if not include_triggered and task.get("triggered", False):
                                continue
                                
                            # 确保datetime是对象
                            if "datetime" in task and isinstance(task["datetime"], str):
                                task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])
                                
                            # 确保时区正确
                            if "datetime" in task and task["datetime"].tzinfo is None:
                                task["datetime"] = config.TZ.localize(task["datetime"])
                                
                            tasks_list.append(task)
                        except Exception as e:
                            print(f"任务解析错误: {str(e)}")
                    
                    # 按日期时间排序
                    return sorted(tasks_list, key=lambda x: x.get("datetime", datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)))
                    
                # 尝试从旧版本存储中获取
                if hasattr(config, 'TASKS_KEY'):
                    tasks_json = r.get(config.TASKS_KEY)
                    if tasks_json:
                        try:
                            stored_tasks = json.loads(tasks_json)
                            
                            # 将旧存储方式迁移到新存储
                            for task in stored_tasks:
                                task_copy = task.copy()
                                if "datetime" in task_copy and isinstance(task_copy["datetime"], datetime.datetime):
                                    task_copy["datetime"] = task_copy["datetime"].isoformat()
                                r.hset(TASKS_KEY, str(task["id"]), json.dumps(task_copy, default=json_serial))
                            
                            # 过滤任务
                            if username:
                                stored_tasks = [task for task in stored_tasks if task.get("owner") == username]
                            
                            if not include_triggered:
                                stored_tasks = [task for task in stored_tasks if not task.get("triggered", False)]
                            
                            # 确保datetime是对象
                            for task in stored_tasks:
                                if "datetime" in task and isinstance(task["datetime"], str):
                                    task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])
                                
                                # 确保时区正确
                                if "datetime" in task and task["datetime"].tzinfo is None:
                                    task["datetime"] = config.TZ.localize(task["datetime"])
                            
                            # 按日期时间排序
                            return sorted(stored_tasks, key=lambda x: x.get("datetime", datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)))
                        except Exception as e:
                            print(f"从列表存储解析任务出错: {str(e)}")
            except Exception as e:
                print(f"从Redis获取任务时出错: {str(e)}")
        
        # 2. 如果Redis获取失败，尝试从数据库获取
        db_session = get_db_session()
        if db_session:
            try:
                query = db_session.query(cls)
                
                # 如果指定了用户名，只查询该用户的任务
                if username:
                    query = query.filter_by(owner=username)
                
                # 如果不包括已触发任务，只查询未触发的任务
                if not include_triggered:
                    query = query.filter_by(triggered=False)
                    
                # 按时间排序
                query = query.order_by(cls.datetime)
                
                # 获取所有任务
                db_tasks = query.all()
                
                # 转换为字典列表
                tasks_list = []
                for task in db_tasks:
                    tasks_list.append({
                        "id": task.id,
                        "title": task.title,
                        "content": task.content,
                        "datetime": task.datetime,
                        "token_name": task.token_name,
                        "owner": task.owner,
                        "triggered": task.triggered
                    })

                return tasks_list
            except Exception as e:
                print(f"从数据库获取任务时出错: {str(e)}")
            finally:
                close_db_session(db_session)
        
        # 3. 最后，如果所有远程存储都失败，从内存获取
        if username:
            return [task for task in tasks if task.get("owner") == username]
        return tasks
    
    @classmethod
    def get_user_tasks(cls, username, include_triggered=True):
        """获取指定用户的任务"""
        user_tasks = []
        
        # 获取Redis客户端
        r = redis_helper.get_client()
        
        # 1. 首先尝试从Redis获取
        if r:
            # 从Redis加载所有任务
            print(f"尝试从Redis中获取任务，使用键名：{TASKS_KEY}")
            all_tasks = r.hgetall(TASKS_KEY)  # 使用类变量TASKS_KEY而不是config.TASKS_HASH_KEY
            print(f"从Redis中获取到任务数量：{len(all_tasks)}")
            
            for task_id, task_json in all_tasks.items():
                try:
                    task = json.loads(task_json)
                    print(f"解析到任务： {task_id} - {task.get('title', '无标题')}")
                    
                    # 检查任务所有者
                    if task.get("owner") == username:
                        # 检查是否包含已触发的任务
                        if include_triggered or not task.get("triggered", False):
                            # 确保datetime是对象
                            if isinstance(task["datetime"], str):
                                # 将ISO格式字符串转回datetime对象（带时区）
                                task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])

                            # 确保时区正确
                            if task["datetime"].tzinfo is None:
                                task["datetime"] = config.TZ.localize(task["datetime"])

                            user_tasks.append(task)
                except Exception as e:
                    print(f"任务解析错误: {str(e)}")

            # 如果没有找到任务，尝试从旧的存储方式获取
            if not user_tasks and hasattr(config, 'TASKS_KEY'):
                print(f"尝试从旧存储中获取，键名：{config.TASKS_KEY}")
                tasks_json = r.get(config.TASKS_KEY)
                if tasks_json:
                    try:
                        all_tasks = json.loads(tasks_json)
                        for task in all_tasks:
                            if task.get("owner") == username:
                                if include_triggered or not task.get("triggered", False):
                                    # 确保datetime是对象
                                    if isinstance(task["datetime"], str):
                                        task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])

                                    # 确保时区正确
                                    if task["datetime"].tzinfo is None:
                                        task["datetime"] = config.TZ.localize(task["datetime"])

                                    # 将任务同时保存到哈希表中
                                    task_copy = task.copy()
                                    task_copy["datetime"] = task_copy["datetime"].isoformat()
                                    r.hset(TASKS_KEY, str(task["id"]), json.dumps(task_copy, default=json_serial))

                                    user_tasks.append(task)
                    except Exception as e:
                        print(f"从列表存储解析任务出错: {str(e)}")
            
            # 如果从Redis获取成功，直接返回
            if user_tasks:
                print(f"从Redis获取到用户 {username} 的任务 {len(user_tasks)} 个")
                return sorted(user_tasks, key=lambda x: x["datetime"])
        
        # 2. 如果Redis获取失败，尝试从数据库获取
        db_session = get_db_session()
        if db_session:
            try:
                query = db_session.query(cls).filter_by(owner=username)
                if not include_triggered:
                    query = query.filter_by(triggered=False)

                db_tasks = query.all()
                for task in db_tasks:
                    task_dict = {
                        "id": task.id,
                        "title": task.title,
                        "content": task.content,
                        "datetime": task.datetime,
                        "token_name": task.token_name,
                        "owner": task.owner,
                        "triggered": task.triggered
                    }

                    # 确保时区正确
                    if task_dict["datetime"].tzinfo is None:
                        task_dict["datetime"] = config.TZ.localize(task_dict["datetime"])

                    user_tasks.append(task_dict)
                
                # 如果从数据库获取成功，直接返回
                if user_tasks:
                    print(f"从数据库获取到用户 {username} 的任务 {len(user_tasks)} 个")
                    return sorted(user_tasks, key=lambda x: x["datetime"])
            except Exception as e:
                print(f"从PostgreSQL加载用户任务错误: {str(e)}")
            finally:
                close_db_session(db_session)
        
        # 3. 如果所有远程存储都失败，尝试从内存获取
        if not user_tasks:
            for task in tasks:
                if task.get("owner") == username:
                    if include_triggered or not task.get("triggered", False):
                        user_tasks.append(task)
            if user_tasks:
                print(f"从内存获取到用户 {username} 的任务 {len(user_tasks)} 个")

        # 按时间排序
        return sorted(user_tasks, key=lambda x: x["datetime"])
    
    @classmethod
    def delete_task(cls, task_id, username=None):
        """删除任务"""
        task_id = int(task_id)
        
        # 1. 首先尝试从Redis删除
        r = redis_helper.get_client()
        
        deleted = False
        
        if r:
            try:
                # 检查任务是否存在
                task_json = r.hget(TASKS_KEY, task_id)
                
                if task_json:
                    task = json.loads(task_json)
                    
                    # 检查权限
                    is_authorized = (not username or task.get("owner") == username)
                    
                    if is_authorized:
                        # 从哈希表中移除任务
                        r.hdel(TASKS_KEY, task_id)
                        
                        # 从用户的任务集合中移除
                        owner = task.get("owner")
                        if owner:
                            user_tasks_key = USER_TASKS_KEY.format(username=owner)
                            r.srem(user_tasks_key, task_id)
                        
                        # 从待处理任务集合中移除
                        r.zrem(PENDING_TASKS_KEY, task_id)
                        
                        # 添加到同步队列，标记为删除
                        from app.utils.sync_manager import SyncManager
                        SyncManager.add_to_sync_queue(
                            'tasks',
                            task_id,
                            task,
                            OP_TYPES['DELETE']
                        )
                        
                        deleted = True
                        print(f"已从Redis删除任务 {task_id}")
            except Exception as e:
                print(f"从Redis删除任务时出错: {str(e)}")
        
        # 2. 尝试从数据库删除
        db_session = get_db_session()
        if db_session:
            try:
                task_query = db_session.query(cls).filter(cls.id == task_id)
                
                # 如果指定了用户名，添加所有者过滤条件
                if username:
                    task_query = task_query.filter(cls.owner == username)
                
                task = task_query.first()
                
                if task:
                    db_session.delete(task)
                    db_session.commit()
                    deleted = True
                    print(f"已从数据库删除任务 {task_id}")
            except Exception as e:
                db_session.rollback()
                print(f"从数据库删除任务时出错: {str(e)}")
            finally:
                close_db_session(db_session)
        
        # 3. 从内存列表中删除（如果有）
        for i, task in enumerate(tasks):
            if task["id"] == task_id:
                # 检查权限
                is_authorized = (not username or task.get("owner") == username)
                
                if is_authorized:
                    tasks.pop(i)
                    deleted = True
                    print(f"已从内存删除任务 {task_id}")
                break
        
        return deleted
    
    @classmethod
    def update_task_status(cls, task_id, triggered=True):
        """更新任务状态（设置为已触发或未触发）"""
        # 确保task_id是整数
        task_id = int(task_id)
        
        # 当前时间戳
        current_timestamp = time.time()
        
        # 1. 获取Redis连接并尝试更新
        r = redis_helper.get_client()
        
        if r:
            # 更新Redis中的任务状态
            # 1.1 获取任务详情
            task_json = r.hget(TASKS_KEY, task_id)
            if not task_json:
                print(f"任务 {task_id} 不存在")
                return False
                
            task = json.loads(task_json)
            
            # 1.2 更新触发状态
            task['triggered'] = triggered
            task['updated_at'] = current_timestamp
            
            # 1.3 保存回Redis
            r.hset(TASKS_KEY, task_id, json.dumps(task, default=json_serial))
            
            # 1.4 从待处理队列中移除（如果已触发）
            if triggered:
                r.zrem(PENDING_TASKS_KEY, task_id)
            else:
                # 如果未触发，重新添加到待处理队列
                task_datetime = datetime.datetime.fromisoformat(task['datetime']) if 'datetime' in task and task['datetime'] else None
                if task_datetime:
                    task_timestamp = task_datetime.timestamp()
                    r.zadd(PENDING_TASKS_KEY, {str(task_id): task_timestamp})
            
            # 1.5 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tasks',
                task_id,
                task,
                OP_TYPES['UPDATE']
            )
            
            print(f"已更新任务 {task_id} 的触发状态为 {triggered}，键名: {TASKS_KEY}")
            return True
        
        # 2. 尝试更新数据库中的任务状态
        db_session = get_db_session()
        if db_session:
            try:
                # 获取任务
                task = db_session.query(cls).get(task_id)
                if not task:
                    print(f"任务 {task_id} 不存在")
                    return False
                    
                # 更新状态
                task.triggered = triggered
                db_session.commit()
                print(f"已更新数据库中任务 {task_id} 的触发状态为 {triggered}")
                return True
            except Exception as e:
                db_session.rollback()
                print(f"更新任务状态时出错: {str(e)}")
                return False
            finally:
                close_db_session(db_session)
        
        # 3. 如果上述方法都失败，更新内存中的任务状态
        for i, task in enumerate(tasks):
            if task["id"] == task_id:
                tasks[i]["triggered"] = triggered
                print(f"已更新内存中任务 {task_id} 的触发状态为 {triggered}")
                return True
                    
        print(f"任务 {task_id} 不存在")
        return False
    
    @classmethod
    def sync_to_postgres(cls):
        """将Redis中的任务同步到数据库"""
        print("正在将任务从Redis同步到PostgreSQL数据库...")
        
        # 检查Redis和数据库是否都可用
        r = redis_helper.get_client()
        if not r:
            print("Redis连接不可用，无法同步")
            return False
            
        db_session = get_db_session()
        if not db_session:
            print("数据库连接不可用，无法同步")
            return False
            
        try:
            # 从Redis获取所有任务
            all_tasks = r.hgetall(TASKS_KEY)
            
            if not all_tasks:
                print("Redis中没有任务可同步")
                return True
                
            # 获取上次同步的时间戳
            last_sync_key = "task_last_sync_timestamp"
            last_sync_time = r.get(last_sync_key)
            last_sync_timestamp = float(last_sync_time) if last_sync_time else 0
            
            # 获取已同步的任务ID记录
            synced_tasks_key = "task_synced_ids"
            synced_tasks = r.smembers(synced_tasks_key) or set()
            synced_tasks = {task_id.decode('utf-8') if isinstance(task_id, bytes) else task_id for task_id in synced_tasks}
            
            # 当前时间戳
            current_timestamp = time.time()
            
            # 同步计数器
            sync_count = 0
            skip_count = 0
            
            # 对每个任务进行处理
            for task_id, task_json in all_tasks.items():
                try:
                    # 将bytes键转换为字符串
                    task_id_str = task_id.decode('utf-8') if isinstance(task_id, bytes) else task_id
                    
                    # 如果任务ID已经在同步记录中，并且没有更新标记，则跳过
                    if task_id_str in synced_tasks:
                        # 检查任务是否有更新
                        task_data = json.loads(task_json)
                        task_updated_at = task_data.get('updated_at', 0)
                        
                        # 如果没有更新时间戳，或者更新时间早于上次同步，则跳过
                        if not task_updated_at or float(task_updated_at) <= last_sync_timestamp:
                            skip_count += 1
                            continue
                    
                    # 解析任务数据
                    task_data = json.loads(task_json)
                    task_id = int(task_id_str)
                    
                    # 解析时间字段
                    if "datetime" in task_data and isinstance(task_data["datetime"], str):
                        task_data["datetime"] = datetime.datetime.fromisoformat(task_data["datetime"])
                        
                    if "created_at" in task_data and isinstance(task_data["created_at"], str):
                        task_data["created_at"] = datetime.datetime.fromisoformat(task_data["created_at"])
                    
                    # 检查任务是否已存在
                    task = db_session.query(cls).filter(cls.id == task_id).first()
                    
                    if task:
                        # 更新现有任务
                        task.title = task_data.get("title", task.title)
                        task.content = task_data.get("content", task.content)
                        task.datetime = task_data.get("datetime", task.datetime)
                        task.token_name = task_data.get("token_name", task.token_name)
                        task.owner = task_data.get("owner", task.owner)
                        task.triggered = task_data.get("triggered", task.triggered)
                        # 不更新created_at字段，保留原始创建时间
                    else:
                        # 创建新任务
                        new_task = cls(
                            id=task_id,
                            title=task_data.get("title", ""),
                            content=task_data.get("content", ""),
                            datetime=task_data.get("datetime", datetime.datetime.now(config.TZ)),
                            token_name=task_data.get("token_name", "默认"),
                            owner=task_data.get("owner", ""),
                            triggered=task_data.get("triggered", False),
                            created_at=task_data.get("created_at", datetime.datetime.now(config.TZ))
                        )
                        db_session.add(new_task)
                    
                    # 将任务ID添加到已同步集合
                    r.sadd(synced_tasks_key, task_id_str)
                    
                    sync_count += 1
                except Exception as e:
                    print(f"同步任务 {task_id} 时出错: {str(e)}")
                    continue
            
            # 提交所有更改
            db_session.commit()
            
            # 更新上次同步时间戳
            r.set(last_sync_key, current_timestamp)
            
            print(f"成功同步 {sync_count} 个任务到数据库，跳过 {skip_count} 个已同步任务")
            return True
            
        except Exception as e:
            db_session.rollback()
            print(f"任务同步过程中出错: {str(e)}")
            return False
        finally:
            close_db_session(db_session)

    @classmethod
    def create_task(cls, title, content, notify_time, token_name, owner):
        """创建任务并保存到内存和Redis"""
        # 生成唯一ID
        task_id = int(uuid.uuid4().int % (2**31 - 1))
        
        # 创建任务字典
        task = {
            "id": task_id,
            "title": title,
            "content": content,
            "datetime": notify_time,
            "token_name": token_name,
            "owner": owner,
            "triggered": False,
            "created_at": datetime.datetime.now(config.TZ)
        }
        
        # 1. 保存到Redis
        r = redis_helper.get_client()
        
        if r:
            # 保存任务到哈希表
            task_copy = task.copy()
            task_copy["datetime"] = task_copy["datetime"].isoformat()
            task_copy["created_at"] = task_copy["created_at"].isoformat()
            
            r.hset(TASKS_KEY, task_id, json.dumps(task_copy, default=json_serial))
            
            # 将任务ID添加到用户的任务集合
            user_tasks_key = USER_TASKS_KEY.format(username=owner)
            r.sadd(user_tasks_key, task_id)
            
            # 将任务ID添加到待处理任务集合（使用任务时间的时间戳作为分数）
            r.zadd(PENDING_TASKS_KEY, {str(task_id): notify_time.timestamp()})
            
            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tasks',
                task_id,
                task_copy,
                OP_TYPES['CREATE']
            )
            
            print(f"任务已保存到Redis，ID: {task_id}")
        else:
            print("Redis连接不可用，任务将只保存在内存中")
            
        # 2. 尝试保存到数据库
        db_session = get_db_session()
        if db_session:
            try:
                # 创建Task实例
                db_task = cls(
                    id=task_id,
                    title=title,
                    content=content,
                    datetime=notify_time,
                    token_name=token_name,
                    owner=owner,
                    triggered=False,
                    created_at=datetime.datetime.now(config.TZ)
                )
                
                # 添加到会话并提交
                db_session.add(db_task)
                db_session.commit()
                print(f"任务已保存到数据库，ID: {task_id}")
            except Exception as e:
                db_session.rollback()
                print(f"保存任务到数据库时出错: {str(e)}")
            finally:
                close_db_session(db_session)
        else:
            print("数据库连接不可用，任务将只保存在Redis和内存中")
            
        # 3. 如果Redis和数据库都不可用，保存到内存
        if not r and not db_session:
            tasks.append(task)
            print(f"任务已保存到内存，ID: {task_id}")
        
        return task_id

    @classmethod
    def get_all_tasks(cls, username=None, include_triggered=True):
        """获取所有任务（如果指定了用户名，则只获取该用户的任务）"""
        # 1. 首先尝试从Redis获取
        r = redis_helper.get_client()
        
        if r:
            try:
                # 检查任务哈希表是否存在
                if r.exists(TASKS_KEY):
                    try:
                        # 获取所有任务
                        all_tasks = r.hgetall(TASKS_KEY)
                        stored_tasks = []
                        
                        for task_id, task_json in all_tasks.items():
                            task = json.loads(task_json)
                            
                            # 如果指定了用户名，只返回该用户的任务
                            if username and task.get("owner") != username:
                                continue
                                
                            # 如果不包括已触发任务，跳过已触发的任务
                            if not include_triggered and task.get("triggered", False):
                                continue
                                
                            # 确保datetime是对象
                            if "datetime" in task and isinstance(task["datetime"], str):
                                task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])
                            
                            # 确保时区正确
                            if "datetime" in task and task["datetime"].tzinfo is None:
                                task["datetime"] = config.TZ.localize(task["datetime"])
                                
                            stored_tasks.append(task)
                            
                        # 按时间排序
                        return sorted(stored_tasks, key=lambda x: x.get("datetime", datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)))
                    except Exception as e:
                        print(f"解析任务时出错: {str(e)}")
                
                # 如果哈希表不存在，尝试旧的存储方式（列表）
                if hasattr(config, 'TASKS_KEY'):
                    tasks_json = r.get(config.TASKS_KEY)
                    if tasks_json:
                        try:
                            stored_tasks = json.loads(tasks_json)
                            
                            # 如果指定了用户名，只返回该用户的任务
                            if username:
                                stored_tasks = [task for task in stored_tasks if task.get("owner") == username]
                                
                            # 如果不包括已触发任务，跳过已触发的任务
                            if not include_triggered:
                                stored_tasks = [task for task in stored_tasks if not task.get("triggered", False)]
                            
                            # 确保datetime是对象
                            for task in stored_tasks:
                                if "datetime" in task and isinstance(task["datetime"], str):
                                    task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])
                                
                                # 确保时区正确
                                if "datetime" in task and task["datetime"].tzinfo is None:
                                    task["datetime"] = config.TZ.localize(task["datetime"])
                            
                            # 按时间排序
                            return sorted(stored_tasks, key=lambda x: x.get("datetime", datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)))
                        except Exception as e:
                            print(f"从列表存储解析任务出错: {str(e)}")
            except Exception as e:
                print(f"从Redis获取任务时出错: {str(e)}")
        
        # 2. 如果Redis获取失败，尝试从数据库获取
        db_session = get_db_session()
        if db_session:
            try:
                query = db_session.query(cls)
                
                # 如果指定了用户名，只查询该用户的任务
                if username:
                    query = query.filter_by(owner=username)
                
                # 如果不包括已触发任务，只查询未触发的任务
                if not include_triggered:
                    query = query.filter_by(triggered=False)
                    
                # 按时间排序
                query = query.order_by(cls.datetime)
                
                # 获取所有任务
                db_tasks = query.all()
                
                # 转换为字典列表
                tasks_list = []
                for task in db_tasks:
                    tasks_list.append({
                        "id": task.id,
                        "title": task.title,
                        "content": task.content,
                        "datetime": task.datetime,
                        "token_name": task.token_name,
                        "owner": task.owner,
                        "triggered": task.triggered
                    })

                return tasks_list
            except Exception as e:
                print(f"从数据库获取任务时出错: {str(e)}")
            finally:
                close_db_session(db_session)
        
        # 3. 最后，如果所有远程存储都失败，从内存获取
        if username:
            return [task for task in tasks if task.get("owner") == username]
        return tasks

    @classmethod
    def get_pending_tasks(cls):
        """获取所有待触发的任务
        从Redis中获取待触发的任务，如果Redis不可用，则从数据库或内存获取
        """
        pending_tasks = []
        now = datetime.datetime.now(config.TZ)
        
        # 1. 首先尝试从Redis获取
        r = redis_helper.get_client()
        if r:
            try:
                # 使用Redis的Sorted Set特性，获取所有分数小于等于当前时间戳的任务ID
                # 这些任务就是需要触发的任务
                current_timestamp = now.timestamp()
                task_ids = r.zrangebyscore(PENDING_TASKS_KEY, 0, current_timestamp)
                
                if task_ids:
                    print(f"找到 {len(task_ids)} 个待触发的任务ID")
                    
                    # 获取这些任务的详细信息
                    for task_id in task_ids:
                        task_id_str = task_id.decode('utf-8') if isinstance(task_id, bytes) else task_id
                        task_json = r.hget(TASKS_KEY, task_id_str)
                        
                        if task_json:
                            try:
                                task = json.loads(task_json)
                                
                                # 检查任务是否已触发
                                if not task.get("triggered", False):
                                    # 确保datetime是对象
                                    if "datetime" in task and isinstance(task["datetime"], str):
                                        try:
                                            task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])
                                        except ValueError:
                                            # 尝试使用其他日期格式
                                            formats = [
                                                "%Y-%m-%dT%H:%M:%S.%f%z",
                                                "%Y-%m-%dT%H:%M:%S%z",
                                                "%Y-%m-%d %H:%M:%S",
                                                "%Y-%m-%d %H:%M"
                                            ]
                                            for fmt in formats:
                                                try:
                                                    task["datetime"] = datetime.datetime.strptime(task["datetime"], fmt)
                                                    break
                                                except ValueError:
                                                    continue
                                    
                                    # 确保时区正确
                                    if "datetime" in task and task["datetime"].tzinfo is None:
                                        task["datetime"] = config.TZ.localize(task["datetime"])
                                    
                                    # 检查任务时间是否已到
                                    if "datetime" in task and task["datetime"] <= now:
                                        pending_tasks.append(task)
                            except Exception as e:
                                print(f"解析任务 {task_id} 数据时出错: {str(e)}")
                                import traceback
                                traceback.print_exc()
                
                # 如果仍然没有找到任务，扫描所有任务
                if not pending_tasks:
                    all_tasks = r.hgetall(TASKS_KEY)
                    for task_id, task_json in all_tasks.items():
                        try:
                            task = json.loads(task_json)
                            
                            # 检查任务是否已触发
                            if not task.get("triggered", False):
                                # 解析日期时间
                                if "datetime" in task and isinstance(task["datetime"], str):
                                    try:
                                        task["datetime"] = datetime.datetime.fromisoformat(task["datetime"])
                                    except ValueError:
                                        # 尝试使用其他日期格式
                                        formats = [
                                            "%Y-%m-%dT%H:%M:%S.%f%z",
                                            "%Y-%m-%dT%H:%M:%S%z",
                                            "%Y-%m-%d %H:%M:%S",
                                            "%Y-%m-%d %H:%M"
                                        ]
                                        for fmt in formats:
                                            try:
                                                task["datetime"] = datetime.datetime.strptime(task["datetime"], fmt)
                                                break
                                            except ValueError:
                                                continue
                                
                                # 确保时区正确
                                if "datetime" in task and task["datetime"].tzinfo is None:
                                    task["datetime"] = config.TZ.localize(task["datetime"])
                                
                                # 检查任务时间是否已到
                                if "datetime" in task and task["datetime"] <= now:
                                    pending_tasks.append(task)
                                    
                                    # 添加到待处理队列
                                    task_timestamp = task["datetime"].timestamp()
                                    r.zadd(PENDING_TASKS_KEY, {str(task["id"]): task_timestamp})
                        except Exception as e:
                            print(f"解析任务 {task_id} 数据时出错: {str(e)}")
                
                # 如果从Redis获取成功，直接返回
                if pending_tasks:
                    print(f"从Redis获取到 {len(pending_tasks)} 个待触发任务")
                    return sorted(pending_tasks, key=lambda x: x["datetime"])
            except Exception as e:
                print(f"从Redis获取待处理任务时出错: {str(e)}")
                import traceback
                traceback.print_exc()
        
        # 2. 如果Redis获取失败，尝试从数据库获取
        db_session = get_db_session()
        if db_session:
            try:
                db_tasks = db_session.query(cls).filter(
                    cls.triggered == False,
                    cls.datetime <= now
                ).order_by(cls.datetime).all()
                
                for task in db_tasks:
                    pending_tasks.append({
                        "id": task.id,
                        "title": task.title,
                        "content": task.content,
                        "datetime": task.datetime,
                        "token_name": task.token_name,
                        "owner": task.owner,
                        "triggered": task.triggered,
                        "db_obj": task  # 保存数据库对象引用，方便后面更新
                    })
                
                print(f"从数据库获取到 {len(pending_tasks)} 个待触发任务")
            except Exception as e:
                print(f"从数据库获取待处理任务时出错: {str(e)}")
                import traceback
                traceback.print_exc()
            finally:
                close_db_session(db_session)
        
        # 3. 最后，如果所有远程存储都失败，从内存获取
        if not pending_tasks:
            for task in tasks:
                # 检查任务是否已触发
                if not task.get("triggered", False):
                    # 确保datetime是对象
                    task_datetime = task.get("datetime")
                    if isinstance(task_datetime, str):
                        try:
                            task_datetime = datetime.datetime.fromisoformat(task_datetime)
                        except ValueError:
                            # 尝试使用其他日期格式
                            formats = [
                                "%Y-%m-%dT%H:%M:%S.%f%z",
                                "%Y-%m-%dT%H:%M:%S%z",
                                "%Y-%m-%d %H:%M:%S",
                                "%Y-%m-%d %H:%M"
                            ]
                            for fmt in formats:
                                try:
                                    task_datetime = datetime.datetime.strptime(task_datetime, fmt)
                                    break
                                except ValueError:
                                    continue
                                    
                    # 确保时区正确
                    if task_datetime and task_datetime.tzinfo is None:
                        task_datetime = config.TZ.localize(task_datetime)
                    
                    # 更新任务中的datetime
                    task["datetime"] = task_datetime
                    
                    # 检查任务时间是否已到
                    if task_datetime and task_datetime <= now:
                        pending_tasks.append(task)
            
            print(f"从内存获取到 {len(pending_tasks)} 个待触发任务")
        
        return sorted(pending_tasks, key=lambda x: x.get("datetime", datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)))

    @classmethod
    def mark_task_triggered(cls, task_id):
        """将任务标记为已触发"""
        task_id = int(task_id)
        
        # 1. 首先更新Redis中的任务状态
        r = redis_helper.get_client()
        
        updated = False
        
        if r:
            try:
                # 获取任务
                task_json = r.hget(TASKS_KEY, task_id)
                
                if task_json:
                    task = json.loads(task_json)
                    
                    # 更新触发状态
                    task["triggered"] = True
                    
                    # 保存回Redis
                    r.hset(TASKS_KEY, task_id, json.dumps(task, default=json_serial))
                    
                    # 从待处理列表中移除
                    r.zrem(PENDING_TASKS_KEY, task_id)
                    
                    # 添加到同步队列
                    from app.utils.sync_manager import SyncManager
                    SyncManager.add_to_sync_queue(
                        'tasks',
                        task_id,
                        task,
                        OP_TYPES['UPDATE']
                    )
                    
                    updated = True
                    print(f"已将Redis中的任务 {task_id} 标记为已触发")
            except Exception as e:
                print(f"更新Redis中的任务状态时出错: {str(e)}")
        
        # 2. 尝试更新数据库中的任务状态
        db_session = get_db_session()
        if db_session:
            try:
                task = db_session.query(cls).filter(cls.id == task_id).first()
                
                if task:
                    task.triggered = True
                    db_session.commit()
                    updated = True
                    print(f"已将数据库中的任务 {task_id} 标记为已触发")
            except Exception as e:
                db_session.rollback()
                print(f"更新数据库中的任务状态时出错: {str(e)}")
            finally:
                close_db_session(db_session)
        
        # 3. 更新内存中的任务状态
        for task in tasks:
            if task["id"] == task_id:
                task["triggered"] = True
                updated = True
                print(f"已将内存中的任务 {task_id} 标记为已触发")
                break
        
        return updated 