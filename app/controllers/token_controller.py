from flask import request, redirect, url_for, flash, g
from app.models.token import Token
from app.config import get_config
from app.utils.redis_helper import redis_helper

config = get_config()

class TokenController:
    """Token控制器"""
    
    @staticmethod
    def add_token():
        """添加或更新息知API令牌"""
        # 获取登录用户
        username = g.user_id
        is_admin = g.is_admin
        
        token_name = request.form.get('token_name', '').strip()
        token_value = request.form.get('token_value', '').strip()
        
        # 管理员可以指定token所有者
        token_owner = request.form.get('token_owner', '').strip()
        if is_admin and token_owner:
            # 管理员指定了token所有者
            owner_username = token_owner
            print(f"管理员 {username} 正在为用户 {owner_username} 添加/更新token: {token_name}")
        else:
            # 普通用户或管理员未指定所有者，使用当前用户
            owner_username = username
            print(f"用户 {username} 正在添加/更新token: {token_name}")
        
        # 验证输入
        if not token_name:
            flash("账号名称不能为空", "danger")
            return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))
        
        if not token_value or not token_value.startswith('XZ'):
            flash("息知令牌格式不正确，应以XZ开头", "danger")
            return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))
        
        # 获取当前所有tokens
        tokens = Token.get_user_tokens(None if is_admin else username)
        print('当前所有的tokens: ' , tokens)
        # 验证是否有权限修改
        if not is_admin:
            # 检查token名是否已存在且不属于当前用户
            if token_name in tokens:
                # 获取token所有者信息
                owner = None
                r = redis_helper.get_client()
                if r:
                    token_info_json = r.hget(f"{config.TOKENS_KEY}:info", token_name)
                    if token_info_json:
                        try:
                            import json
                            token_info = json.loads(token_info_json)
                            owner = token_info.get('owner')
                            print(f"Token {token_name} 当前所有者: {owner}")
                        except Exception as e:
                            print(f"解析token信息出错: {str(e)}")
                
                # 如果token已存在且所有者不是当前用户，禁止修改
                if owner and owner != username:
                    flash(f"通知账号'{token_name}'已被其他用户使用，请使用其他名称", "danger")
                    return redirect(url_for('token.user_tokens'))
        
        # 检查是否已存在同名账号
        is_new = token_name not in tokens
        
        # 更新内存中的tokens字典
        tokens[token_name] = token_value
        
        # 保存到数据库或Redis
        r = redis_helper.get_client()
        
        # Redis存储（如果可用）
        if r:
            try:
                print("开始保存息知token...")
                
                # 1. 清理操作，确保数据一致性
                print("清理旧数据...")
                # 如果是已存在的token，检查之前的所有者并从其集合中移除
                if not is_new:
                    token_info_json = r.hget(f"{config.TOKENS_KEY}:info", token_name)
                    if token_info_json:
                        try:
                            import json
                            token_info = json.loads(token_info_json)
                            old_owner = token_info.get('owner')
                            if old_owner and old_owner != username:
                                # 从旧所有者的集合中移除
                                print(f"从旧所有者 {old_owner} 的集合中移除token {token_name}")
                                old_user_tokens_key = f"user:{old_owner}:tokens"
                                r.srem(old_user_tokens_key, token_name)
                        except Exception as e:
                            print(f"清理旧关联出错: {str(e)}")
                
                # 2. 直接设置token值
                print(f"保存token值: {token_name}={token_value[:10]}...")
                r.hset(config.TOKENS_KEY, token_name, token_value)
                
                # 3. 设置token所有者信息
                import json
                import datetime
                token_info = {
                    'owner': owner_username,
                    'created_at': datetime.datetime.now(config.TZ).isoformat()
                }
                print(f"保存token所有者信息: {token_name} -> {owner_username}")
                r.hset(f"{config.TOKENS_KEY}:info", token_name, json.dumps(token_info))
                
                # 4. 记录用户拥有的token集合
                user_tokens_key = f"user:{owner_username}:tokens"
                print(f"添加到用户集合: {user_tokens_key} 添加 {token_name}")
                r.sadd(user_tokens_key, token_name)
                
                # 5. 检查添加后的状态
                updated_token_value = r.hget(config.TOKENS_KEY, token_name)
                updated_token_info = r.hget(f"{config.TOKENS_KEY}:info", token_name)
                updated_user_tokens = r.smembers(user_tokens_key)
                
                print(f"保存后检查:")
                print(f"- token值: {token_name}={updated_token_value[:10]}...")
                print(f"- token信息: {updated_token_info}")
                print(f"- 用户集合: {updated_user_tokens}")
                
                if is_new:
                    flash(f"已添加新通知账号「{token_name}」", "success")
                else:
                    flash(f"已更新通知账号「{token_name}」的令牌", "success")
                
                # 6. 重新加载用户tokens
                refreshed_tokens = Token.get_user_tokens(username)
                print(f"刷新后用户 {username} 的tokens: {refreshed_tokens}")
                
                return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))
            except Exception as e:
                print(f"保存token时发生错误: {str(e)}")
                flash(f"保存通知账号时出错，请重试", "danger")
                return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))
        else:
            # PostgreSQL或内存存储
            if Token.save_tokens(tokens):
                if is_new:
                    flash(f"已添加新通知账号「{token_name}」", "success")
                else:
                    flash(f"已更新通知账号「{token_name}」的令牌", "success")
            else:
                flash(f"保存账号失败，请检查数据库连接", "danger")
        
        return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))
    
    @staticmethod
    def delete_token(token_name):
        """删除息知API令牌"""
        # 获取登录用户
        username = g.user_id
        is_admin = g.is_admin

        # 不允许删除"默认"账号
        if token_name == '默认' and not is_admin:
            flash("不能删除默认通知账号", "danger")
            return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))

        # 检查是否有权限删除
        if not is_admin:
            # 检查token所有者
            owner = None
            r = redis_helper.get_client()
            if r:
                token_info_json = r.hget(f"{config.TOKENS_KEY}:info", token_name)
                if token_info_json:
                    try:
                        import json
                        info = json.loads(token_info_json)
                        owner = info.get('owner')
                    except Exception as e:
                        print(f"解析token信息出错: {str(e)}")

                if owner != username:
                    flash("您只能删除自己创建的通知账号", "danger")
                    return redirect(url_for('token.user_tokens'))
            else:
                flash("您只能删除自己创建的通知账号", "danger")
                return redirect(url_for('token.user_tokens'))
                
        # 获取当前所有tokens
        tokens = Token.get_user_tokens(None)

        # 从字典中删除
        if token_name in tokens:
            del tokens[token_name]

            # 如果Redis可用，直接从Redis删除
            r = redis_helper.get_client()
            if r:
                # 1. 删除token值
                r.hdel(config.TOKENS_KEY, token_name)

                # 2. 删除token所有者信息
                r.hdel(f"{config.TOKENS_KEY}:info", token_name)

                # 3. 从用户拥有的token集合中删除
                if not is_admin and owner:
                    user_tokens_key = f"user:{owner}:tokens"
                    r.srem(user_tokens_key, token_name)

                # 4. 如果是管理员操作，需要检查是否需要从原始所有者的集合中删除
                elif is_admin and owner:
                    user_tokens_key = f"user:{owner}:tokens"
                    r.srem(user_tokens_key, token_name)

                flash(f"已删除通知账号「{token_name}」", "success")
                return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens'))

            # 否则尝试通过save_tokens保存
            if Token.save_tokens(tokens):
                flash(f"已删除通知账号「{token_name}」", "success")
            else:
                flash(f"删除账号失败，请检查数据库连接", "danger")
        else:
            flash(f"通知账号「{token_name}」不存在", "warning")

        return redirect(url_for('token.user_tokens' if not is_admin else 'token.manage_tokens')) 