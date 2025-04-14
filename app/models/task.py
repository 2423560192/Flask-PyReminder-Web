import json
import datetime
import os
import yaml
from sqlalchemy import Column, Integer, String, Boolean, Text
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from app.utils.db import Base, get_db_session, close_db_session
from app.utils.constants import OP_TYPES
from app.utils.helpers import parse_datetime
from app.config import get_config
from app.utils.redis_helper import redis_helper
from flask import current_app

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
TASKS_KEY = config.TASKS_HASH_KEY  # 哈希表，存储所有任务的详细信息
TASK_ID_KEY = config.TASK_ID_KEY   # 任务ID计数器
USER_TASKS_KEY = "cache:user:{username}:tasks"  # 集合，存储用户拥有的任务ID
PENDING_TASKS_KEY = "cache:tasks:pending"  # 有序集合，存储待处理任务，分数为触发时间的时间戳

# 打印使用的键名
print(f"使用的Redis键名: TASKS_KEY={TASKS_KEY}, TASK_ID_KEY={TASK_ID_KEY}")

class Task(Base):
    """任务模型"""
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=True)
    datetime = Column(DateTime(timezone=True), nullable=False)
    token_name = Column(String(50), nullable=False)
    owner = Column(String(50), nullable=False)
    triggered = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

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
        
        # 构建任务数据
        task = {
            "id": task_id,
            "title": title,
            "content": content,
            "datetime": task_datetime.isoformat() if task_datetime else None,
            "token_name": token_name,
            "owner": username,
            "triggered": False,
            "created_at": datetime.datetime.now(config.TZ).isoformat()
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
    def get_all_tasks(cls, username=None):
        """获取所有任务"""
        # 获取Redis客户端
        r = redis_helper.get_client()
        
        # 1. 首先尝试从Redis获取
        if r:
            # 从Redis哈希表获取所有任务
            all_tasks_hash = r.hgetall(TASKS_KEY)
            if all_tasks_hash:
                # 如果哈希表中有任务
                tasks_list = []
                for task_id, task_json in all_tasks_hash.items():
                    try:
                        task_dict = json.loads(task_json)
                        tasks_list.append(task_dict)
                    except Exception as e:
                        print(f"解析任务 {task_id} 出错: {str(e)}")
                
                # 如果提供了用户名，则过滤任务
                if username:
                    filtered_tasks = [task for task in tasks_list if task.get("owner") == username]
                    return filtered_tasks
                return tasks_list
                
            # 尝试从旧的存储方式获取
            if hasattr(config, 'TASKS_KEY'):
                tasks_json = r.get(config.TASKS_KEY)
                if tasks_json:
                    try:
                        all_tasks = json.loads(tasks_json)
                        # 如果提供了用户名，则过滤任务
                        if username:
                            return [task for task in all_tasks if task.get("owner") == username]
                        return all_tasks
                    except Exception as e:
                        print(f"解析旧任务存储出错: {str(e)}")
        
        # 2. 如果Redis获取失败，尝试从数据库获取
        db_session = get_db_session()
        if db_session:
            try:
                # 如果提供了用户名，则获取该用户的任务；否则获取所有任务
                if username:
                    tasks_query = db_session.query(cls).filter(cls.owner == username).all()
                else:
                    tasks_query = db_session.query(cls).all()

                # 转换为字典列表以保持兼容性
                tasks_list = []
                for task in tasks_query:
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
        # 确保task_id是整数
        task_id = int(task_id)
        
        # 1. 获取Redis连接并尝试删除
        r = redis_helper.get_client()
        
        if r:
            # 从Redis删除
            # 1.1 获取任务详情
            task_json = r.hget(TASKS_KEY, task_id)
            if not task_json:
                print(f"任务 {task_id} 不存在")
                return False
                
            task = json.loads(task_json)
            task_owner = task.get('owner')
            
            # 1.2 检查权限（只有任务所有者或管理员可以删除）
            if username and username != task_owner and not username.lower() == 'admin':
                print(f"用户 {username} 无权删除任务 {task_id}")
                return False
                
            # 1.3 从Redis中删除任务
            r.hdel(TASKS_KEY, task_id)
            
            # 1.4 从用户的任务集合中删除
            if task_owner:
                user_tasks_key = USER_TASKS_KEY.format(username=task_owner)
                r.srem(user_tasks_key, task_id)
                
            # 1.5 从待处理队列中删除
            r.zrem(PENDING_TASKS_KEY, task_id)
            
            # 1.6 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tasks',
                task_id,
                task,
                OP_TYPES['DELETE']
            )
            
            print(f"已删除任务 {task_id}")
            return True
        
        # 2. 尝试从数据库删除
        db_session = get_db_session()
        if db_session:
            try:
                # 获取任务
                task = db_session.query(cls).get(task_id)
                if not task:
                    print(f"任务 {task_id} 不存在")
                    return False
                    
                # 检查权限
                if username and username != task.owner and not username.lower() == 'admin':
                    print(f"用户 {username} 无权删除任务 {task_id}")
                    return False
                    
                # 删除任务
                db_session.delete(task)
                db_session.commit()
                print(f"已从数据库删除任务 {task_id}")
                return True
            except Exception as e:
                db_session.rollback()
                print(f"删除任务时出错: {str(e)}")
                return False
            finally:
                close_db_session(db_session)
        
        # 3. 如果上述方法都失败，从内存删除
        for i, task in enumerate(tasks):
            if task["id"] == task_id:
                # 检查权限
                if username and username != task.get("owner") and not username.lower() == 'admin':
                    print(f"用户 {username} 无权删除任务 {task_id}")
                    return False
                    
                # 删除任务
                tasks.pop(i)
                print(f"已从内存删除任务 {task_id}")
                return True
                    
        print(f"任务 {task_id} 不存在")
        return False
    
    @classmethod
    def update_task_status(cls, task_id, triggered=True):
        """更新任务状态（设置为已触发或未触发）"""
        # 确保task_id是整数
        task_id = int(task_id)
        
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
    def sync_to_postgres(cls, redis_client=None, session=None):
        """将Redis中的任务数据同步到PostgreSQL"""
        # 1. 获取Redis连接
        redis = redis_client or redis_helper.get_client()
        
        # 如果没有Redis连接，无法同步
        if not redis:
            print("无法获取Redis连接，同步取消")
            return
        
        # 2. 获取数据库会话
        db_session = session
        local_session = False
        
        if db_session is None:
            # 获取新的数据库会话
            db_session = get_db_session()
            local_session = True
            
            # 如果无法获取数据库会话，无法同步
            if not db_session:
                print("无法获取数据库会话，同步取消")
                return
        
        try:
            # 获取所有任务数据
            tasks = redis.hgetall(TASKS_KEY)
            
            for task_id, task_json in tasks.items():
                try:
                    task_data = json.loads(task_json)
                    
                    # 检查任务是否已存在
                    existing_task = db_session.query(cls).filter_by(id=task_id).first()
                    
                    if existing_task:
                        # 更新现有任务
                        existing_task.title = task_data['title']
                        existing_task.description = task_data.get('description', '')
                        existing_task.owner = task_data['owner']
                        existing_task.token_id = task_data.get('token_id')
                        existing_task.notify_time = datetime.datetime.fromisoformat(task_data['notify_time'])
                        existing_task.is_completed = task_data.get('is_completed', False)
                        if 'created_at' in task_data:
                            existing_task.created_at = datetime.datetime.fromisoformat(task_data['created_at'])
                    else:
                        # 创建新任务
                        new_task = cls(
                            id=task_id,
                            title=task_data['title'],
                            description=task_data.get('description', ''),
                            owner=task_data['owner'],
                            token_id=task_data.get('token_id'),
                            notify_time=datetime.datetime.fromisoformat(task_data['notify_time']),
                            is_completed=task_data.get('is_completed', False),
                            created_at=datetime.datetime.fromisoformat(task_data.get('created_at', datetime.datetime.now(config.TZ).isoformat()))
                        )
                        db_session.add(new_task)
                        
                except Exception as e:
                    print(f"同步任务 {task_id} 时出错: {str(e)}")
                    continue
                    
            db_session.commit()
            print("任务数据同步完成")
            
        except Exception as e:
            db_session.rollback()
            print(f"任务数据同步失败: {str(e)}")
        finally:
            # 只关闭本地创建的会话
            if local_session:
                close_db_session(db_session) 