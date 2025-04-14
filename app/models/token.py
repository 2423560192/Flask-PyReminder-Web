import os
import json
import yaml
import datetime
from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from app.utils.db import Base, get_db_session, close_db_session
from app.utils.constants import OP_TYPES
from app.config import get_config
from app.utils.redis_helper import redis_helper

config = get_config()

# 内存tokens字典，作为备用存储
memory_tokens = {"默认": config.DEFAULT_NOTIFICATION_TOKEN}
memory_token_owners = {"默认": "system"}

# Redis键名
TOKENS_KEY = "tokens"  # 存储所有令牌的哈希表
TOKEN_INFO_KEY = "tokens:info"  # 存储令牌信息的哈希表
TOKEN_OWNER_KEY = "user:{username}:tokens"  # 存储用户拥有的令牌


class Token(Base):
    """息知API通知令牌模型"""
    __tablename__ = 'tokens'

    # Redis键名
    TOKENS_KEY = "tokens"  # 存储所有令牌的哈希表
    TOKEN_INFO_KEY = "tokens:info"  # 存储令牌信息的哈希表
    TOKEN_OWNER_KEY = "user:{username}:tokens"  # 存储用户拥有的令牌

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)
    token = Column(String(200), nullable=False)
    owner = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.now)

    def __repr__(self):
        return f'<Token {self.name}>'

    def __init__(self, name, token, owner):
        self.name = name
        self.token = token
        self.owner = owner

    @classmethod
    def load_tokens(cls):
        """加载息知token配置"""
        tokens_dict = {}
        default_token = config.DEFAULT_NOTIFICATION_TOKEN

        try:
            # 优先尝试从Redis加载
            r = redis_helper.get_client()
            if r:
                redis_tokens = r.hgetall(cls.TOKENS_KEY)
                if redis_tokens:
                    print(f"从Redis加载了{len(redis_tokens)}个通知账号")
                    return redis_tokens

            # 其次尝试从PostgreSQL加载
            if db_available:
                tokens = db_session.query(cls).all()
                if tokens:
                    for token in tokens:
                        tokens_dict[token.name] = token.token

                        # 同时也保存到Redis缓存
                        if r:
                            r.hset(cls.TOKENS_KEY, token.name, token.token)

                            # 保存令牌信息
                            token_info = {
                                'owner': token.owner or 'system',
                                'created_at': token.created_at.isoformat() if token.created_at else datetime.datetime.now(config.TZ).isoformat()
                            }
                            r.hset(cls.TOKEN_INFO_KEY, token.name, json.dumps(token_info))

                            # 保存用户拥有的令牌
                            if token.owner:
                                r.sadd(cls.TOKEN_OWNER_KEY.format(username=token.owner), token.name)

                    print(f"从PostgreSQL加载了{len(tokens)}个通知账号")
                    return tokens_dict

            # 如果数据库中没有数据，尝试从文件加载
            if os.path.exists(config.TOKENS_FILE):
                with open(config.TOKENS_FILE, 'r', encoding='utf-8') as file:
                    config_data = yaml.safe_load(file)
                    tokens_dict = config_data.get('tokens', {})

                    # 如果从文件加载成功，保存到Redis缓存
                    if r:
                        for name, value in tokens_dict.items():
                            r.hset(cls.TOKENS_KEY, name, value)

                            # 保存令牌信息（默认为系统所有）
                            token_info = {
                                'owner': 'system',
                                'created_at': datetime.datetime.now(config.TZ).isoformat()
                            }
                            r.hset(cls.TOKEN_INFO_KEY, name, json.dumps(token_info))

                        print(f"已将{len(tokens_dict)}个通知账号从文件同步到Redis")

                    return tokens_dict
        except Exception as e:
            print(f"加载token配置失败: {str(e)}")

        # 默认token
        tokens_dict = {"默认": default_token}

        # 保存默认token到Redis
        if r:
            r.hset(cls.TOKENS_KEY, "默认", default_token)

            # 保存默认令牌信息
            token_info = {
                'owner': 'system',
                'created_at': datetime.datetime.now(config.TZ).isoformat()
            }
            r.hset(cls.TOKEN_INFO_KEY, "默认", json.dumps(token_info))

        else:
            # 使用内存存储
            memory_tokens["默认"] = default_token
            memory_token_owners["默认"] = "system"
            print("使用内存存储模式，已加载默认通知账号")

        return tokens_dict

    @classmethod
    def add_token(cls, name, token, owner=None):
        """添加新令牌或更新现有令牌"""
        created_at = datetime.datetime.now(config.TZ)

        # 构建令牌数据
        token_data = {
            'name': name,
            'token': token,
            'owner': owner,
            'created_at': created_at.isoformat()
        }

        if r:
            # 保存到Redis
            r.hset(cls.TOKENS_KEY, name, token)

            # 保存令牌信息
            token_info = {
                'owner': owner or 'system',
                'created_at': created_at.isoformat()
            }
            r.hset(cls.TOKEN_INFO_KEY, name, json.dumps(token_info))

            # 如果指定了所有者，将令牌添加到用户的令牌集合中
            if owner:
                r.sadd(cls.TOKEN_OWNER_KEY.format(username=owner), name)

            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tokens',
                name,
                token_data,
                OP_TYPES['CREATE']
            )

            return True

        elif db_available:
            # 直接保存到数据库
            try:
                # 检查令牌是否已存在
                token = db_session.query(cls).filter_by(name=name).first()

                if token:
                    # 更新现有令牌
                    token.token = token
                    if owner:
                        token.owner = owner
                else:
                    # 创建新令牌
                    new_token = cls(
                        name=name,
                        token=token,
                        owner=owner,
                        created_at=created_at
                    )
                    db_session.add(new_token)

                db_session.commit()
                return True
            except Exception as e:
                db_session.rollback()
                print(f"保存令牌到数据库失败: {str(e)}")
                return False
        else:
            # 使用内存存储
            memory_tokens[name] = token
            memory_token_owners[name] = owner or 'system'
            return True

    @classmethod
    def delete_token(cls, name):
        """删除令牌"""
        if r:
            # 检查令牌是否存在
            if not r.hexists(cls.TOKENS_KEY, name):
                return False

            # 获取令牌所有者信息
            token_info_json = r.hget(cls.TOKEN_INFO_KEY, name)
            token_info = json.loads(token_info_json) if token_info_json else {'owner': 'system'}
            owner = token_info.get('owner')

            # 构建令牌数据（用于同步队列）
            token_data = {
                'name': name,
                'owner': owner,
            }

            # 从Redis删除令牌
            r.hdel(cls.TOKENS_KEY, name)
            r.hdel(cls.TOKEN_INFO_KEY, name)

            # 如果有所有者，从用户的令牌集合中删除
            if owner and owner != 'system':
                r.srem(cls.TOKEN_OWNER_KEY.format(username=owner), name)

            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tokens',
                name,
                token_data,
                OP_TYPES['DELETE']
            )

            return True

        elif db_available:
            # 直接从数据库删除
            try:
                token = db_session.query(cls).filter_by(name=name).first()
                if not token:
                    return False

                db_session.delete(token)
                db_session.commit()
                return True
            except Exception as e:
                db_session.rollback()
                print(f"从数据库删除令牌失败: {str(e)}")
                return False
        else:
            # 从内存删除
            if name not in memory_tokens:
                return False

            del memory_tokens[name]
            if name in memory_token_owners:
                del memory_token_owners[name]

            return True

    @classmethod
    def get_user_tokens(cls, username=None):
        """获取用户可用的通知账号"""
        tokens = {}

        # 获取Redis客户端
        r = redis_helper.get_client()

        print('是否有redis：', r)

        if r:
            try:
                # 从Redis获取所有token
                all_tokens = r.hgetall(cls.TOKENS_KEY)
                if all_tokens:
                    # 获取token信息
                    token_info = {}
                    for name in all_tokens.keys():
                        info_json = r.hget(cls.TOKEN_INFO_KEY, name)
                        if info_json:
                            try:
                                info = json.loads(info_json)
                                token_info[name] = info
                            except json.JSONDecodeError:
                                print(f"无法解析token信息: {info_json}")
                                continue

                    for name, token in all_tokens.items():
                        # 获取token的所有者信息
                        info = token_info.get(name, {'owner': 'system'})
                        owner = info.get('owner', 'system')

                        # 如果是管理员或token所有者，则添加token
                        if username is None or owner == username:
                            tokens[name] = token.decode('utf-8') if isinstance(token, bytes) else token

                    print(f"从Redis加载了{len(tokens)}个token: {list(tokens.keys())}")
                else:
                    print("Redis中没有找到任何token")

                    # 如果Redis中没有token，从数据库加载
                    session = get_db_session()
                    if session:
                        all_tokens = session.query(cls).all()
                        if all_tokens:
                            for token in all_tokens:
                                if username is None or token.owner == username:
                                    tokens[token.name] = token.token

                                    # 保存到Redis
                                    r.hset(cls.TOKENS_KEY, token.name, token.token)
                                    r.hset(cls.TOKEN_INFO_KEY, token.name, json.dumps({
                                        'owner': token.owner,
                                        'created_at': token.created_at.isoformat()
                                    }))

                            print(f"从数据库加载了{len(tokens)}个token: {list(tokens.keys())}")
            except Exception as e:
                print(f"从Redis获取token失败: {str(e)}")
                # 如果Redis失败，尝试从数据库加载
                session = get_db_session()
                if session:
                    all_tokens = session.query(cls).all()
                    if all_tokens:
                        for token in all_tokens:
                            if username is None or token.owner == username:
                                tokens[token.name] = token.token
                        print(f"从数据库加载了{len(tokens)}个token: {list(tokens.keys())}")

        # 如果Redis和数据库都不可用，使用内存存储
        if not tokens and memory_tokens:
            for name, token in memory_tokens.items():
                owner = memory_token_owners.get(name, 'system')
                if username is None or owner == username:
                    tokens[name] = token
            print(f"从内存加载了{len(tokens)}个token: {list(tokens.keys())}")

        return tokens

    @classmethod
    def set_token_owner(cls, token_name, owner):
        """设置token所有者"""
        if r:
            # 检查token是否存在
            if not r.hexists(cls.TOKENS_KEY, token_name):
                print(f"在Redis中未找到token {token_name}")
                return False

            # 获取旧所有者信息
            old_owner = None
            token_info_json = r.hget(cls.TOKEN_INFO_KEY, token_name)
            if token_info_json:
                token_info = json.loads(token_info_json)
                old_owner = token_info.get('owner')

            # 设置新的所有者信息
            token_info = {
                'owner': owner,
                'created_at': datetime.datetime.now(config.TZ).isoformat()
            }
            r.hset(cls.TOKEN_INFO_KEY, token_name, json.dumps(token_info))

            # 如果有旧所有者，从旧所有者的集合中移除
            if old_owner and old_owner != 'system':
                r.srem(cls.TOKEN_OWNER_KEY.format(username=old_owner), token_name)

            # 将令牌添加到新所有者的集合中
            if owner and owner != 'system':
                r.sadd(cls.TOKEN_OWNER_KEY.format(username=owner), token_name)

            # 获取令牌值用于同步队列
            token_value = r.hget(cls.TOKENS_KEY, token_name)

            # 构建令牌数据用于同步
            token_data = {
                'name': token_name,
                'token': token_value,
                'owner': owner,
                'created_at': token_info['created_at']
            }

            # 添加到同步队列
            from app.utils.sync_manager import SyncManager
            SyncManager.add_to_sync_queue(
                'tokens',
                token_name,
                token_data,
                OP_TYPES['UPDATE']
            )

            print(f"已在Redis中设置token {token_name} 的所有者为 {owner}")
            return True

        elif db_available:
            # 直接在数据库中设置
            try:
                token = db_session.query(cls).filter_by(name=token_name).first()
                if not token:
                    print(f"在数据库中未找到token {token_name}")
                    return False

                token.owner = owner
                db_session.commit()
                print(f"已在数据库中设置token {token_name} 的所有者为 {owner}")
                return True
            except Exception as e:
                db_session.rollback()
                print(f"设置token所有者时出错: {str(e)}")
                return False
        else:
            # 在内存中设置
            if token_name not in memory_tokens:
                print(f"在内存中未找到token {token_name}")
                return False

            memory_token_owners[token_name] = owner
            print(f"已在内存中设置token {token_name} 的所有者为 {owner}")
            return True

    @classmethod
    def sync_to_postgres(cls, redis, db_session):
        """将Redis中的令牌数据同步到PostgreSQL"""
        if not redis or not db_available:
            return

        try:
            # 获取所有令牌数据
            tokens = redis.hgetall(cls.TOKENS_KEY)

            for token_id, token_json in tokens.items():
                try:
                    token_data = json.loads(token_json)

                    # 检查令牌是否已存在
                    existing_token = db_session.query(cls).filter_by(id=token_id).first()

                    if existing_token:
                        # 更新现有令牌
                        existing_token.name = token_data['name']
                        existing_token.owner = token_data['owner']
                        existing_token.token = token_data['token']
                        if 'created_at' in token_data:
                            existing_token.created_at = datetime.datetime.fromisoformat(token_data['created_at'])
                    else:
                        # 创建新令牌
                        new_token = cls(
                            id=token_id,
                            name=token_data['name'],
                            token=token_data['token'],
                            owner=token_data['owner'],
                            created_at=datetime.datetime.fromisoformat(
                                token_data.get('created_at', datetime.datetime.now(config.TZ).isoformat()))
                        )
                        db_session.add(new_token)

                except Exception as e:
                    print(f"同步令牌 {token_id} 时出错: {str(e)}")
                    continue

            db_session.commit()
            print("令牌数据同步完成")

        except Exception as e:
            db_session.rollback()
            print(f"令牌数据同步失败: {str(e)}")
