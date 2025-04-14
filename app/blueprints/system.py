from flask import Blueprint, render_template, flash, redirect, url_for
from app.controllers.system_controller import SystemController
from app.utils.helpers import admin_required
from app.config import get_config
from app.models.user import User
from app.utils.redis_helper import redis_helper

# 创建系统蓝图
system_bp = Blueprint('system', __name__, url_prefix='/system')

@system_bp.route('/info')
@admin_required
def system_info():
    """系统信息页面(仅管理员)"""
    # 获取配置和当前时间
    config = get_config()
    now = config.get_now()
    
    system_data = SystemController.get_system_info()
    
    # 确保redis_connected变量传递给模板
    redis_connected = system_data.get('redis_connected', False)
    
    return render_template('system/info.html', 
                           system_data=system_data, 
                           now=now,
                           redis_connected=redis_connected)

@system_bp.route('/users')
@admin_required
def manage_users():
    """用户管理页面(仅管理员)"""
    # 获取所有用户
    users = User.get_all_users()
    
    # 获取配置和当前时间
    config = get_config()
    now = config.get_now()
    
    # 获取Redis连接状态
    r = redis_helper.get_client()
    
    return render_template('system/users.html',
                           users=users,
                           now=now,
                           redis_connected=r is not None)

@system_bp.route('/users/set_admin/<username>/<int:is_admin>', methods=['POST'])
@admin_required
def set_admin(username, is_admin):
    """设置用户管理员权限(仅管理员)"""
    # 将is_admin转换为布尔值
    is_admin_bool = bool(is_admin)
    
    # 设置用户管理员权限
    if User.set_admin(username, is_admin_bool):
        flash(f'已{"设置" if is_admin_bool else "取消"} {username} 的管理员权限', 'success')
    else:
        flash(f'{"设置" if is_admin_bool else "取消"}管理员权限失败', 'danger')
    
    return redirect(url_for('system.manage_users')) 