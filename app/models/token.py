import os
import json
import yaml
import datetime
import time
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
TOKENS_KEY = config.TOKENS_KEY  # 使用配置文件中定义的键名
TOKEN_INFO_KEY = f"{TOKENS_KEY}:info"  # 存储令牌信息的哈希表
TOKEN_OWNER_KEY = "user:{username}:tokens"  # 存储用户拥有的令牌


class Token(Base):
    """息知API通知令牌模型"""
    __tablename__ = 'tokens'

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
    def get_tokens_key(cls):
        """获取令牌存储键名"""
        return TOKENS_KEY
        
    @classmethod
    def get_token_info_key(cls):
        """获取令牌信息键名"""
        return TOKEN_INFO_KEY
        
    @classmethod
    def get_token_owner_key(cls, username):
        """获取用户令牌集合键名"""
        return TOKEN_OWNER_KEY.format(username=username)

    @classmethod
    def load_tokens(cls):
        """加载息知token配置"""
        tokens_dict = {}
        default_token = config.DEFAULT_NOTIFICATION_TOKEN

        try:
            # 优先尝试从Redis加载
            r = redis_helper.get_client()
            if r:
                redis_tokens = r.hgetall(cls.get_tokens_key())
                if redis_tokens:
                    print(f"从Redis加载了{len(redis_tokens)}个通知账号")
                    return redis_tokens

            # 其次尝试从数据库加载
            session = get_db_session()
            if session:
                tokens = session.query(cls).all()
                if tokens:
                    for token in tokens:
                        tokens_dict[token.name] = token.token

                        # 同时也保存到Redis缓存
                        if r:
                            r.hset(cls.get_tokens_key(), token.name, token.token)

                            # 保存令牌信息
                            token_info = {
                                'owner': token.owner or 'system',
                                'created_at': token.created_at.isoformat() if token.created_at else datetime.datetime.now(config.TZ).isoformat()
                            }
                            r.hset(cls.get_token_info_key(), token.name, json.dumps(token_info))

                            # 保存用户拥有的令牌
                            if token.owner:
                                r.sadd(cls.get_token_owner_key(token.owner), token.name)

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
                            r.hset(cls.get_tokens_key(), name, value)

                            # 保存令牌信息（默认为系统所有）
                            token_info = {
                                'owner': 'system',
                                'created_at': datetime.datetime.now(config.TZ).isoformat()
                            }
                            r.hset(cls.get_token_info_key(), name, json.dumps(token_info))

                        print(f"已将{len(tokens_dict)}个通知账号从文件同步到Redis")

                    return tokens_dict
        except Exception as e:
            print(f"加载token配置失败: {str(e)}")

        # 默认token
        tokens_dict = {"默认": default_token}

        # 保存默认token到Redis
        if r:
            r.hset(cls.get_tokens_key(), "默认", default_token)

            # 保存默认令牌信息
            token_info = {
                'owner': 'system',
                'created_at': datetime.datetime.now(config.TZ).isoformat()
            }
            r.hset(cls.get_token_info_key(), "默认", json.dumps(token_info))

        else:
            # 使用内存存储
            memory_tokens["默认"] = default_token
            memory_token_owners["默认"] = "system"
            print("使用内存存储模式，已加载默认通知账号")

        return tokens_dict

    @classmethod
    def add_token(cls, name, token, username=None):
        """添加一个新的通知令牌"""
        # 首先尝试保存到Redis
        r = redis_helper.get_client()
        if r:
            try:
                # 保存令牌
                r.hset(cls.get_tokens_key(), name, token)
                
                # 保存令牌所有者信息
                if username:
                    # 将令牌添加到用户的令牌集合中
                    r.sadd(cls.get_token_owner_key(username), name)
                    
                # 保存令牌创建时间
                token_info = {
                    'owner': username or 'system',
                    'created_at': datetime.utcnow().isoformat()
                }
                r.hset(cls.get_token_info_key(), name, json.dumps(token_info))
                
                print(f"已添加令牌到Redis: {name}")
            except Exception as e:
                print(f"添加令牌到Redis失败: {str(e)}")
        
        # 尝试保存到数据库
        try:
            session = get_db_session()
            if session:
                # 检查令牌是否已存在
                existing_token = session.query(cls).filter_by(name=name).first()
                if existing_token:
                    # 更新现有令牌
                    existing_token.token = token
                    existing_token.owner = username or 'system'
                    existing_token.updated_at = datetime.utcnow()
                else:
                    # 创建新令牌
                    new_token = cls(
                        name=name,
                        token=token,
                        owner=username or 'system',
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow()
                    )
                    session.add(new_token)
                session.commit()
                print(f"已添加令牌到数据库: {name}")
            else:
                print("数据库会话获取失败，无法保存令牌到数据库")
        except Exception as e:
            print(f"添加令牌到数据库失败: {str(e)}")
            if session:
                session.rollback()
        
        # 保存到内存（作为备份）
        cls.in_memory_tokens[name] = token
        cls.in_memory_token_info[name] = {
            'owner': username or 'system',
            'created_at': datetime.utcnow().isoformat()
        }
        print(f"已添加令牌到内存: {name}")
        
        return True

    @classmethod
    def delete_token(cls, name, username=None):
        """删除指定的通知令牌"""
        success = False
        
        # 权限检查 - 只有令牌所有者或管理员才能删除
        if name == "默认":
            print("默认令牌不能被删除")
            return False
            
        r = redis_helper.get_client()
        
        # 检查令牌所有权
        owner = None
        if r:
            try:
                # 从Redis获取令牌信息
                token_info_json = r.hget(cls.get_token_info_key(), name)
                if token_info_json:
                    token_info = json.loads(token_info_json)
                    owner = token_info.get('owner')
            except Exception as e:
                print(f"从Redis获取令牌信息失败: {str(e)}")
        
        # 如果无法从Redis获取所有者信息，尝试从数据库获取
        if not owner:
            try:
                session = get_db_session()
                if session:
                    token_obj = session.query(cls).filter_by(name=name).first()
                    if token_obj:
                        owner = token_obj.owner
            except Exception as e:
                print(f"从数据库获取令牌信息失败: {str(e)}")
        
        # 如果还是获取不到，尝试从内存获取
        if not owner and name in cls.in_memory_token_info:
            owner = cls.in_memory_token_info[name].get('owner')
            
        # 检查权限（只有令牌所有者或管理员可以删除）
        if username and owner and owner != 'system' and owner != username:
            print(f"权限不足，用户 {username} 不能删除其他用户的令牌")
            return False
            
        # 从Redis中删除令牌
        if r:
            try:
                # 从总令牌表中删除
                r.hdel(cls.get_tokens_key(), name)
                # 从令牌信息表中删除
                r.hdel(cls.get_token_info_key(), name)
                # 从用户令牌集合中删除
                if owner:
                    r.srem(cls.get_token_owner_key(owner), name)
                success = True
                print(f"已从Redis删除令牌: {name}")
            except Exception as e:
                print(f"从Redis删除令牌失败: {str(e)}")
        
        # 从数据库中删除令牌
        try:
            session = get_db_session()
            if session:
                token_obj = session.query(cls).filter_by(name=name).first()
                if token_obj:
                    session.delete(token_obj)
                    session.commit()
                    success = True
                    print(f"已从数据库删除令牌: {name}")
                else:
                    print(f"数据库中找不到令牌: {name}")
            else:
                print("数据库会话获取失败，无法从数据库删除令牌")
        except Exception as e:
            print(f"从数据库删除令牌失败: {str(e)}")
            if session:
                session.rollback()
        
        # 从内存中删除令牌
        if name in cls.in_memory_tokens:
            del cls.in_memory_tokens[name]
            if name in cls.in_memory_token_info:
                del cls.in_memory_token_info[name]
            success = True
            print(f"已从内存删除令牌: {name}")
        
        return success

    @classmethod
    def get_user_tokens(cls, username, include_default=True):
        """获取用户拥有的令牌"""
        tokens = {}

        # 优先从Redis获取
        r = redis_helper.get_client()
        if r:
            try:
                # 获取用户令牌列表
                token_names = r.smembers(cls.get_token_owner_key(username))
                if token_names:
                    # 获取所有令牌详情
                    for name in token_names:
                        token_value = r.hget(cls.get_tokens_key(), name)
                        if token_value:
                            tokens[name] = token_value

                # 添加默认令牌
                if include_default:
                    default_token_value = r.hget(cls.get_tokens_key(), "默认")
                    if default_token_value:
                        tokens["默认"] = default_token_value
            except Exception as e:
                print(f"从Redis获取用户令牌时出错: {str(e)}")
        
        # 如果Redis获取失败或没有令牌，尝试从数据库获取
        if not tokens:
            try:
                session = get_db_session()
                if session:
                    user_tokens = session.query(cls).filter_by(owner=username).all()
                    for token in user_tokens:
                        tokens[token.name] = token.token
                    
                    # 添加默认令牌
                    if include_default:
                        default_token = session.query(cls).filter_by(name="默认").first()
                        if default_token:
                            tokens["默认"] = default_token.token
                        else:
                            # 如果数据库中没有默认令牌，使用配置中的默认令牌
                            tokens["默认"] = config.DEFAULT_NOTIFICATION_TOKEN
                    
                    # 将获取到的令牌也同步到Redis
                    if r and tokens:
                        for name, value in tokens.items():
                            r.hset(cls.get_tokens_key(), name, value)
                            if name != "默认":
                                r.sadd(cls.get_token_owner_key(username), name)
            except Exception as e:
                print(f"从数据库获取用户令牌时出错: {str(e)}")
                
        # 如果还是没有令牌，从内存获取
        if not tokens:
            print(f"警告：无法从Redis或数据库获取用户令牌，将使用内存存储")
            
            # 获取所有内存中的令牌
            all_tokens = cls.in_memory_tokens
            
            # 筛选出属于用户的令牌
            for name, info in cls.in_memory_token_info.items():
                if info.get('owner') == username:
                    token_value = all_tokens.get(name)
                    if token_value:
                        tokens[name] = token_value
            
            # 添加默认令牌
            if include_default and "默认" in all_tokens:
                tokens["默认"] = all_tokens["默认"]
                
        # 如果还是没有默认令牌，添加配置中的默认令牌
        if include_default and "默认" not in tokens:
            tokens["默认"] = config.DEFAULT_NOTIFICATION_TOKEN
            
        return tokens

    @classmethod
    def set_token_owner(cls, token_name, owner):
        """设置token所有者"""
        r = redis_helper.get_client()
        if r:
            # 检查token是否存在
            if not r.hexists(cls.get_tokens_key(), token_name):
                print(f"在Redis中未找到token {token_name}")
                return False

            # 获取旧所有者信息
            old_owner = None
            token_info_json = r.hget(cls.get_token_info_key(), token_name)
            if token_info_json:
                token_info = json.loads(token_info_json)
                old_owner = token_info.get('owner')

            # 设置新的所有者信息
            token_info = {
                'owner': owner,
                'created_at': datetime.datetime.now(config.TZ).isoformat()
            }
            r.hset(cls.get_token_info_key(), token_name, json.dumps(token_info))

            # 如果有旧所有者，从旧所有者的集合中移除
            if old_owner and old_owner != 'system':
                r.srem(cls.get_token_owner_key(old_owner), token_name)

            # 将令牌添加到新所有者的集合中
            if owner and owner != 'system':
                r.sadd(cls.get_token_owner_key(owner), token_name)

            # 获取令牌值用于同步队列
            token_value = r.hget(cls.get_tokens_key(), token_name)

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

        else:
            # 尝试在数据库中设置
            session = get_db_session()
            if session:
                try:                    
                    token = session.query(cls).filter_by(name=token_name).first()
                    if not token:
                        print(f"在数据库中未找到token {token_name}")
                        return False

                    token.owner = owner
                    session.commit()
                    print(f"已在数据库中设置token {token_name} 的所有者为 {owner}")
                    return True
                except Exception as e:
                    session.rollback()
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
    def sync_to_postgres(cls):
        """将Redis中的令牌数据同步到PostgreSQL"""
        print("开始同步令牌数据到数据库...")
        
        # 获取Redis和数据库连接
        redis = redis_helper.get_client()
        if not redis:
            print("Redis连接不可用，无法同步令牌数据")
            return False
            
        db_session = get_db_session()
        if not db_session:
            print("数据库会话不可用，无法同步令牌数据")
            return False

        try:
            # 获取所有令牌数据
            tokens = redis.hgetall(cls.get_tokens_key())
            if not tokens:
                print("Redis中没有找到任何令牌数据")
                return True
                
            # 同步计数器
            sync_count = 0

            for token_name, token_value in tokens.items():
                try:
                    # 获取令牌信息
                    token_info_json = redis.hget(cls.get_token_info_key(), token_name)
                    token_info = json.loads(token_info_json) if token_info_json else {'owner': 'system'}
                    
                    # 转换字节类型为字符串
                    if isinstance(token_name, bytes):
                        token_name = token_name.decode('utf-8')
                    if isinstance(token_value, bytes):
                        token_value = token_value.decode('utf-8')
                    
                    # 检查令牌是否已存在
                    existing_token = db_session.query(cls).filter_by(name=token_name).first()

                    if existing_token:
                        # 更新现有令牌
                        existing_token.token = token_value
                        existing_token.owner = token_info.get('owner', 'system')
                        if 'created_at' in token_info:
                            try:
                                existing_token.created_at = datetime.datetime.fromisoformat(token_info['created_at'])
                            except Exception:
                                existing_token.created_at = datetime.datetime.now(config.TZ)
                    else:
                        # 创建新令牌
                        new_token = cls(
                            name=token_name,
                            token=token_value,
                            owner=token_info.get('owner', 'system')
                        )
                        if 'created_at' in token_info:
                            try:
                                new_token.created_at = datetime.datetime.fromisoformat(token_info['created_at'])
                            except Exception:
                                pass  # 使用默认创建时间
                                
                        db_session.add(new_token)
                        
                    sync_count += 1

                except Exception as e:
                    print(f"同步令牌 {token_name} 时出错: {str(e)}")
                    continue

            # 提交所有更改
            db_session.commit()
            print(f"成功同步 {sync_count} 个令牌到数据库")
            return True

        except Exception as e:
            db_session.rollback()
            print(f"令牌数据同步失败: {str(e)}")
            return False
        finally:
            close_db_session(db_session)

    @classmethod
    def get_token(cls, token_name):
        """获取指定名称的token的值"""
        if not token_name:
            return None
            
        # 从Redis获取
        r = redis_helper.get_client()
        if r:
            try:
                # 直接从Redis获取token
                token_value = r.hget(cls.get_tokens_key(), token_name)
                if token_value:
                    if isinstance(token_value, bytes):
                        return token_value.decode('utf-8')
                    return token_value
            except Exception as e:
                print(f"从Redis获取token失败: {str(e)}")
                
        # 从数据库获取
        session = get_db_session()
        if session:
            try:
                token_obj = session.query(cls).filter_by(name=token_name).first()
                if token_obj:
                    return token_obj.token
            except Exception as e:
                print(f"从数据库获取token失败: {str(e)}")
            finally:
                close_db_session(session)
                
        # 从内存获取
        if token_name in memory_tokens:
            return memory_tokens[token_name]
            
        # 使用默认token
        if token_name == '默认':
            return config.DEFAULT_NOTIFICATION_TOKEN
            
        print(f"未找到名为 {token_name} 的token")
        return None
