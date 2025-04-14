from flask import Blueprint
from app.controllers.task_controller import TaskController
from app.utils.helpers import login_required

# 创建任务蓝图
task_bp = Blueprint('task', __name__, url_prefix='/task')

@task_bp.route('/add', methods=['POST'])
@login_required
def add_task():
    """添加任务"""
    return TaskController.add_task()

@task_bp.route('/delete/<int:task_id>', methods=['POST'])
@login_required
def delete_task(task_id):
    """删除任务"""
    return TaskController.delete_task(task_id)

@task_bp.route('/get')
@login_required
def get_tasks():
    """获取任务列表"""
    return TaskController.get_tasks() 