from flask import Blueprint, render_template
from app.controllers.system_controller import SystemController
from app.utils.helpers import admin_required

# 创建系统蓝图
system_bp = Blueprint('system', __name__, url_prefix='/system')

@system_bp.route('/info')
@admin_required
def system_info():
    """系统信息页面(仅管理员)"""
    system_data = SystemController.get_system_info()
    return render_template('system/info.html', system_data=system_data) 