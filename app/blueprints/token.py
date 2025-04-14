from flask import Blueprint, render_template, g
from app.models.token import Token
from app.controllers.token_controller import TokenController
from app.config import get_config
from app.utils.helpers import login_required, admin_required
from app.utils.redis_helper import redis_helper

config = get_config()

# 创建Token蓝图
token_bp = Blueprint('token', __name__, url_prefix='/token')

@token_bp.route('/manage')
@admin_required
def manage_tokens():
    """息知API令牌管理页面(仅管理员)"""
    # 获取每个token的所有者信息
    tokens = Token.get_user_tokens(None)
    token_owners = {}
    r = redis_helper.get_client()
    if r:
        for token_name in tokens:
            owner_info = r.hget(f"{config.TOKENS_KEY}:info", token_name)
            if owner_info:
                try:
                    import json
                    info = json.loads(owner_info)
                    token_owners[token_name] = info.get('owner', '系统')
                except:
                    token_owners[token_name] = '未知'
            else:
                token_owners[token_name] = '系统'

    return render_template('token/manage.html', tokens=tokens, token_owners=token_owners,
                        redis_connected=r is not None)

@token_bp.route('/user')
@login_required
def user_tokens():
    """用户管理自己的息知API令牌"""
    # 获取登录用户信息
    username = g.user_id
    is_admin = g.is_admin
    
    # 获取用户tokens
    user_tokens = Token.get_user_tokens(username)
    
    # 获取Redis连接状态
    r = redis_helper.get_client()
    
    # 传递额外信息到模板
    template_data = {
        'tokens': user_tokens,
        'username': username,
        'redis_connected': r is not None,
        'now': config.get_now(),
        'token_count': len(user_tokens),
        'is_admin': is_admin
    }
    
    return render_template('token/user.html', **template_data)

@token_bp.route('/add', methods=['POST'])
@login_required
def add_token():
    """添加或更新息知API令牌"""
    return TokenController.add_token()

@token_bp.route('/delete/<token_name>', methods=['POST'])
@login_required
def delete_token(token_name):
    """删除息知API令牌"""
    return TokenController.delete_token(token_name) 