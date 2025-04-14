from flask import Blueprint, render_template
from app.controllers.system_controller import SystemController
from app.utils.helpers import admin_required
from app.config import get_config

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