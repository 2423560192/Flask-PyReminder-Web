from flask import Blueprint, render_template, g
from app.models.task import Task
from app.models.token import Token
from app.utils.helpers import login_required
from app.config import get_config

config = get_config()

# 创建主蓝图
main_bp = Blueprint('main', __name__)

@main_bp.route('/')
@login_required
def index():
    """首页"""
    # 获取登录用户信息
    username = g.user_id
    is_admin = g.is_admin
    
    # 加载系统时间（用于模板中的年份显示）
    now = config.get_now()
    
    # 获取用户可以使用的token
    available_tokens = Token.get_user_tokens(None if is_admin else username)
    print(f"用户 {username} 在首页可使用的通知账号: {list(available_tokens.keys())}")
    
    # 获取任务列表（强制刷新，确保从Redis获取最新状态）
    # 这里只需要修改方法调用，确保使用的是Redis中的最新数据
    all_tasks = Task.get_all_tasks(username=None if is_admin else username, include_triggered=True, force_refresh=True)
    
    return render_template('index.html',
                        tasks=all_tasks,
                        tokens=available_tokens,
                        redis_connected=config.REDIS_URL is not None,
                        username=username,
                        is_admin=is_admin,
                        now=now) 