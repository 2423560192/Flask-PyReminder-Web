import hashlib
import json
import datetime
from flask import g
from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from app.utils.db import Base, get_db_session, close_db_session
from app.utils.constants import OP_TYPES
from app.config import get_config
from app.utils.redis_helper import redis_helper

config = get_config()

# 内存用户字典，作为最后的备用存储
memory_users = {}

# Redis键名
USERS_KEY = "users"  # 存储所有用户的哈希表
USER_ID_COUNTER = "user:id_counter"  # 用户ID计数器


class User(Base):
    """用户模型"""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

    def __repr__(self):
        return f'<User {self.username}>'

    @classmethod
    def create_user(cls, username, password, is_admin=False):
        """创建新用户"""
        # 检查用户名是否已存在
        # 密码加密存储
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        created_at = datetime.datetime.now(config.TZ)

        # 获取Redis连接
        redis = redis_helper.get_client()
        if not redis:
            print("Redis连接不可用")
            return False

        try:
            # 检查用户名是否已存在
            if redis.hexists(USERS_KEY, username):
                return False  # 用户名已存在

            # 用户数据
            user_data = {
                'username': username,
                'password_hash': password_hash,
                'is_admin': is_admin,
                'created_at': created_at.isoformat()
            }

            # 保存到Redis
            redis.hset(USERS_KEY, username, json.dumps(user_data))
            print(f"用户 {username} 已保存到Redis")

            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'users',
                username,
                user_data,
                OP_TYPES['CREATE']
            )

            return True

        except Exception as e:
            print(f"创建用户错误: {str(e)}")
            return False

    @classmethod
    def verify_user(cls, username, password):
        """验证用户凭据"""
        # 管理员账户验证（固定在配置中）
        if username == config.ADMIN_USERNAME:
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            return password_hash == config.ADMIN_PASSWORD_HASH, True

        # 普通用户验证
        password_hash = hashlib.sha256(password.encode()).hexdigest()

        # 获取Redis连接
        redis = redis_helper.get_client()
        if redis:
            # 从Redis获取用户
            user_json = redis.hget(USERS_KEY, username)
            if not user_json:
                return False, False

            user_data = json.loads(user_json)
            return password_hash == user_data['password_hash'], user_data.get('is_admin', False)

        # 从数据库获取用户
        session = get_db_session()
        if session:
            try:
                user = session.query(cls).filter_by(username=username).first()
                if not user:
                    return False, False

                return password_hash == user.password_hash, user.is_admin
            finally:
                close_db_session(session)

        # 从内存获取用户
        if username not in memory_users:
            return False, False

        user_data = memory_users[username]
        return password_hash == user_data['password_hash'], user_data.get('is_admin', False)

    @classmethod
    def update_user(cls, username, data):
        """更新用户信息"""
        # 获取Redis连接
        redis = redis_helper.get_client()
        if redis:
            # 从Redis获取用户
            user_json = redis.hget(USERS_KEY, username)
            if not user_json:
                return False

            user_data = json.loads(user_json)

            # 更新数据
            for key, value in data.items():
                if key in ['password_hash', 'is_admin']:
                    user_data[key] = value

            # 保存回Redis
            redis.hset(USERS_KEY, username, json.dumps(user_data))

            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'users',
                username,
                user_data,
                OP_TYPES['UPDATE']
            )

            return True

        # 从数据库更新用户
        session = get_db_session()
        if session:
            try:
                user = session.query(cls).filter_by(username=username).first()
                if not user:
                    return False

                # 更新数据
                for key, value in data.items():
                    if key == 'password_hash':
                        user.password_hash = value
                    elif key == 'is_admin':
                        user.is_admin = value

                session.commit()
                return True
            except Exception as e:
                session.rollback()
                print(f"更新用户错误: {str(e)}")
                return False
            finally:
                close_db_session(session)

        # 从内存更新用户
        if username not in memory_users:
            return False

        # 更新数据
        for key, value in data.items():
            if key in ['password_hash', 'is_admin']:
                memory_users[username][key] = value

        return True

    @classmethod
    def delete_user(cls, username):
        """删除用户"""
        # 获取Redis连接
        redis = redis_helper.get_client()
        if redis:
            # 检查用户是否存在
            if not redis.hexists(USERS_KEY, username):
                return False

            # 获取用户数据用于同步
            user_json = redis.hget(USERS_KEY, username)
            user_data = json.loads(user_json) if user_json else {}

            # 从Redis删除
            redis.hdel(USERS_KEY, username)

            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'users',
                username,
                user_data,
                OP_TYPES['DELETE']
            )

            return True

        # 从数据库删除用户
        session = get_db_session()
        if session:
            try:
                user = session.query(cls).filter_by(username=username).first()
                if not user:
                    return False

                session.delete(user)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                print(f"删除用户错误: {str(e)}")
                return False
            finally:
                close_db_session(session)

        # 从内存删除用户
        if username not in memory_users:
            return False

        del memory_users[username]
        return True

    @classmethod
    def get_all_users(cls):
        """获取所有用户"""
        users = []

        # 获取Redis连接
        redis = redis_helper.get_client()
        if redis:
            # 从Redis获取所有用户
            all_users = redis.hgetall(USERS_KEY)
            for username, user_json in all_users.items():
                user_data = json.loads(user_json)
                users.append(user_data)
            return users

        # 从数据库获取所有用户
        session = get_db_session()
        if session:
            try:
                db_users = session.query(cls).all()
                for user in db_users:
                    users.append({
                        'id': user.id,
                        'username': user.username,
                        'is_admin': user.is_admin,
                        'created_at': user.created_at.isoformat() if user.created_at else None
                    })
                return users
            finally:
                close_db_session(session)

        # 从内存获取所有用户
        for username, user_data in memory_users.items():
            users.append(user_data)
        return users

    @classmethod
    def get_user(cls, username):
        """获取单个用户"""
        # 获取Redis连接
        redis = redis_helper.get_client()
        if redis:
            # 从Redis获取用户
            user_json = redis.hget(USERS_KEY, username)
            if not user_json:
                return None

            return json.loads(user_json)

        # 从数据库获取用户
        session = get_db_session()
        if session:
            try:
                user = session.query(cls).filter_by(username=username).first()
                if not user:
                    return None

                return {
                    'id': user.id,
                    'username': user.username,
                    'password_hash': user.password_hash,
                    'is_admin': user.is_admin,
                    'created_at': user.created_at.isoformat() if user.created_at else None
                }
            finally:
                close_db_session(session)

        # 从内存获取用户
        return memory_users.get(username)

    @classmethod
    def sync_to_postgres(cls):
        """将Redis中的用户数据同步到MySQL"""
        print("开始同步用户数据到数据库...")
        
        # 获取Redis和数据库连接
        redis = redis_helper.get_client()
        if not redis:
            print("Redis连接不可用，无法同步用户数据")
            return False
            
        db_session = get_db_session()
        if not db_session:
            print("数据库会话不可用，无法同步用户数据")
            return False

        try:
            # 获取所有用户数据
            users = redis.hgetall(USERS_KEY)
            print(f"从Redis获取到 {len(users)} 个用户")
            
            # 同步计数器
            sync_count = 0

            for username, user_json in users.items():
                try:
                    # 解析用户数据
                    if isinstance(username, bytes):
                        username = username.decode('utf-8')
                        
                    user_data = json.loads(user_json)
                    print(f"正在同步用户: {username}")

                    # 检查用户是否已存在
                    existing_user = db_session.query(cls).filter_by(username=username).first()

                    if existing_user:
                        print(f"更新现有用户: {username}")
                        # 更新现有用户
                        existing_user.password_hash = user_data['password_hash']
                        existing_user.is_admin = user_data.get('is_admin', False)
                        if 'created_at' in user_data:
                            try:
                                existing_user.created_at = datetime.datetime.fromisoformat(user_data['created_at'])
                            except ValueError as e:
                                print(f"解析created_at时间出错: {str(e)}")
                                existing_user.created_at = datetime.datetime.now(config.TZ)
                    else:
                        print(f"创建新用户: {username}")
                        # 创建新用户
                        try:
                            created_at = datetime.datetime.fromisoformat(
                                user_data.get('created_at', datetime.datetime.now(config.TZ).isoformat())
                            )
                        except ValueError as e:
                            print(f"解析created_at时间出错: {str(e)}")
                            created_at = datetime.datetime.now(config.TZ)

                        new_user = cls(
                            username=username,
                            password_hash=user_data['password_hash'],
                            is_admin=user_data.get('is_admin', False),
                            created_at=created_at
                        )
                        db_session.add(new_user)
                        
                    sync_count += 1

                except Exception as e:
                    print(f"处理用户 {username} 时出错: {str(e)}")
                    continue

            # 提交所有更改
            db_session.commit()
            print(f"成功同步 {sync_count} 个用户到数据库")
            return True

        except Exception as e:
            print(f"用户数据同步失败: {str(e)}")
            db_session.rollback()
            return False
        finally:
            close_db_session(db_session)
